using System.Formats.Asn1;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace VsceSign.Crypto;

/// <summary>
/// CRL / OCSP revocation checking. Microsoft's vsce-sign only performs this when
/// <c>--requirerevocationstatus</c> is passed; the default path skips it, exactly
/// as this implementation does. When enabled we fetch the CRL distribution
/// points advertised by the certificate and look for the serial number, and
/// additionally consult OCSP responders. Failures to fetch are reported but do
/// not turn into CertificateRevoked: only a positive revocation does.
/// </summary>
internal static class RevocationChecker
{
    public static string? Evaluate(
        X509Chain builtChain,
        List<X509Certificate2>? extraCertificates,
        TextWriter diagnostics,
        bool verbose)
    {
        foreach (var element in builtChain.ChainElements)
        {
            var certificate = element.Certificate;
            if (certificate.Subject.Equals(certificate.Issuer, StringComparison.Ordinal)) continue;

            var crlPoints = GetUrls(certificate, "2.5.29.31");
            var ocspPoints = GetUrls(certificate, "1.3.6.1.5.5.7.1.1");

            if (verbose)
                diagnostics.WriteLine($"Revocation check for {certificate.Subject} (CRL: {string.Join(", ", crlPoints)}, OCSP: {string.Join(", ", ocspPoints)})");

            foreach (var url in crlPoints)
            {
                var crl = TryDownload(url, verbose, diagnostics);
                if (crl is null) continue;
                var verdict = CrlContainsSerial(crl, certificate, out var reason);
                if (verdict is null) continue;
                if (verdict == true) return $"certificate '{certificate.Subject}' is listed in the CRL at {url} ({reason})";
            }

            foreach (var url in ocspPoints)
            {
                var response = TryDownload(url, verbose, diagnostics);
                if (response is null) continue;
                var status = OcspStatus.TryRead(response, certificate, out var message);
                if (status == OcspCertificateStatus.Revoked) return $"certificate '{certificate.Subject}' is revoked according to the OCSP responder at {url} ({message})";
            }
        }

        return null;
    }

    private static byte[]? TryDownload(string url, bool verbose, TextWriter diagnostics)
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
            return client.GetByteArrayAsync(url).GetAwaiter().GetResult();
        }
        catch (Exception ex)
        {
            if (verbose) diagnostics.WriteLine($"  revocation information unavailable from {url}: {ex.Message}");
            return null;
        }
    }

    /// <summary>
    /// Returns true when the CRL revokes the certificate, false when it explicitly
    /// does not, and null when the CRL is unusable (wrong issuer, unreadable, ...).
    /// </summary>
    private static bool? CrlContainsSerial(byte[] crlDer, X509Certificate2 certificate, out string reason)
    {
        reason = string.Empty;
        try
        {
            var crl = Der.Sequence(crlDer);
            var tbs = crl.ReadSequence();
            if (tbs.HasData && tbs.PeekTag().HasSameClassAndValue(Asn1Tag.Integer)) tbs.ReadInteger(); // version
            var signatureAlgorithm = Der.ReadAlgorithmIdentifier(ref tbs);
            var issuer = tbs.ReadEncodedValue().ToArray();
            Der.ReadTime(ref tbs);
            if (tbs.HasData && tbs.PeekTag().HasSameClassAndValue(Asn1Tag.UtcTime) ||
                tbs.HasData && tbs.PeekTag().HasSameClassAndValue(Asn1Tag.GeneralizedTime))
                Der.ReadTime(ref tbs);

            if (!issuer.AsSpan().SequenceEqual(certificate.IssuerName.RawData))
            {
                reason = "the CRL was issued by a different authority";
                return null;
            }

            if (tbs.HasData && tbs.PeekTag().HasSameClassAndValue(Asn1Tag.Sequence))
            {
                var revoked = tbs.ReadSequence();
                while (revoked.HasData)
                {
                    var entry = revoked.ReadSequence();
                    var serial = Der.TrimLeadingZeros(entry.ReadIntegerBytes().ToArray());
                    var date = Der.ReadTime(ref entry);
                    reason = $"revoked on {date:u}";
                    if (serial.AsSpan().SequenceEqual(CertificateSerial(certificate))) return true;
                }
            }
            return false;
        }
        catch (Exception ex) when (ex is AsnContentException or CryptographicException)
        {
            reason = ex.Message;
            return null;
        }
    }

    private static byte[] CertificateSerial(X509Certificate2 certificate) =>
        Der.TrimLeadingZeros(certificate.SerialNumberBytes.ToArray());

    private static List<string> GetUrls(X509Certificate2 certificate, string extensionOid)
    {
        var result = new List<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var extension in certificate.Extensions)
        {
            if (extension.Oid?.Value != extensionOid) continue;
            var raw = Der.UnwrapOctetString(extension.RawData);
            try
            {
                var outer = Der.Sequence(raw);
                while (outer.HasData)
                {
                    var entry = outer.ReadSequence();
                    if (extensionOid == "2.5.29.31")
                    {
                        // DistributionPoint ::= SEQUENCE { [0] distributionPoint, [1] reasons, [2] cRLIssuer }
                        if (entry.HasData && entry.PeekTag().HasSameClassAndValue(Der.ContextConstructed(0)))
                        {
                            var dp = entry.ReadSequence(Der.ContextConstructed(0));
                            if (dp.HasData && dp.PeekTag().HasSameClassAndValue(Der.ContextConstructed(0)))
                                CollectGeneralNames(dp.ReadSequence(Der.ContextConstructed(0)), result, seen);
                        }
                    }
                    else
                    {
                        // AccessDescription ::= SEQUENCE { accessMethod OID, accessLocation GeneralName }
                        entry.ReadObjectIdentifier();
                        CollectGeneralNames(entry, result, seen);
                    }
                }
            }
            catch (AsnContentException)
            {
            }
        }
        return result;
    }

    private static void CollectGeneralNames(AsnReader reader, List<string> urls, HashSet<string> seen)
    {
        // GeneralName ::= CHOICE { [6] uniformResourceIdentifier IA5String, ... }
        while (reader.HasData)
        {
            var tag = reader.PeekTag();
            if (tag.HasSameClassAndValue(Der.ContextPrimitive(6)))
            {
                var raw = reader.ReadEncodedValue().ToArray();
                var (contentOffset, contentLength) = ReadHeader(raw);
                var url = System.Text.Encoding.ASCII.GetString(raw, contentOffset, contentLength);
                if (url.StartsWith("http", StringComparison.OrdinalIgnoreCase) && seen.Add(url)) urls.Add(url);
            }
            else
            {
                reader.ReadEncodedValue();
            }
        }
    }

    private static (int ContentOffset, int ContentLength) ReadHeader(byte[] data)
    {
        int offset = 1;
        int length = data[offset++];
        if ((length & 0x80) != 0)
        {
            int count = length & 0x7f;
            length = 0;
            for (int i = 0; i < count; i++) length = (length << 8) | data[offset++];
        }
        return (offset, length);
    }
}

internal enum OcspCertificateStatus
{
    Good,
    Revoked,
    Unknown,
}

internal static class OcspStatus
{
    public static OcspCertificateStatus? TryRead(byte[] responseDer, X509Certificate2 certificate, out string message)
    {
        message = string.Empty;
        try
        {
            var outer = Der.Sequence(responseDer);
            var envelope = outer.ReadSequence();
            var responseType = envelope.ReadObjectIdentifier();
            if (responseType != "1.3.6.1.5.5.7.48.1.1") return null;
            var basicDer = envelope.ReadOctetString();
            envelope.ThrowIfNotEmpty();

            var basic = Der.Sequence(basicDer);
            var tbs = basic.ReadSequence();
            if (tbs.HasData && tbs.PeekTag().HasSameClassAndValue(Der.ContextConstructed(0)))
            {
                var version = tbs.ReadSequence(Der.ContextConstructed(0));
                version.ReadInteger();
                version.ThrowIfNotEmpty();
            }
            tbs.ReadObjectIdentifier(); // responder id (byName) is handled leniently below
            var genTime = Der.ReadTime(ref tbs);

            var responses = tbs.ReadSequence();
            while (responses.HasData)
            {
                var single = responses.ReadSequence();
                var certId = single.ReadSequence();
                Der.ReadAlgorithmIdentifier(ref certId);
                certId.ReadOctetString(); // issuer name hash
                certId.ReadOctetString(); // issuer key hash
                var serial = Der.TrimLeadingZeros(certId.ReadIntegerBytes().ToArray());
                certId.ThrowIfNotEmpty();
                var status = single.ReadSequence();
                var statusTag = status.ReadEncodedValue();
                var (kind, when) = ReadStatus(statusTag);
                if (serial.AsSpan().SequenceEqual(Der.TrimLeadingZeros(certificate.SerialNumberBytes.ToArray())))
                {
                    message = kind == OcspCertificateStatus.Revoked ? $"revoked at {when:u}" : "good";
                    return kind;
                }
                while (single.HasData) single.ReadEncodedValue();
            }
            return OcspCertificateStatus.Unknown;
        }
        catch (Exception ex) when (ex is AsnContentException or CryptographicException)
        {
            message = ex.Message;
            return null;
        }
    }

    private static (OcspCertificateStatus, DateTimeOffset?) ReadStatus(ReadOnlyMemory<byte> encoded)
    {
        var status = Der.Sequence(encoded);
        var tag = status.PeekTag();
        if (tag.HasSameClassAndValue(Der.ContextPrimitive(0))) return (OcspCertificateStatus.Good, null);
        if (tag.HasSameClassAndValue(Der.ContextConstructed(1)))
        {
            var revoked = status.ReadSequence(Der.ContextConstructed(1));
            var time = Der.ReadTime(ref revoked);
            return (OcspCertificateStatus.Revoked, time);
        }
        return (OcspCertificateStatus.Unknown, null);
    }
}
