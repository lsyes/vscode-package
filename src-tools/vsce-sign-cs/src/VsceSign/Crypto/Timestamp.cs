using System.Formats.Asn1;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace VsceSign.Crypto;

internal enum TimestampCheck
{
    Ok,
    /// <summary>No RFC 3161 timestamp token is attached to the signature.</summary>
    Missing,
    /// <summary>The timestamp token is malformed.</summary>
    Malformed,
    /// <summary>The signature on the timestamp token is invalid.</summary>
    SignatureInvalid,
    /// <summary>The timestamp does not cover this signature (imprint mismatch).</summary>
    ImprintMismatch,
    /// <summary>The timestamp token does not use the timestamping EKU correctly.</summary>
    InvalidEku,
}

internal sealed class TimestampInfo
{
    public TimestampCheck Check { get; init; }
    public string Detail { get; init; } = string.Empty;
    public DateTimeOffset? GenTime { get; init; }
    public X509Certificate2? SignerCertificate { get; init; }
    public List<X509Certificate2> Certificates { get; } = new();
    public SignedData? Token { get; init; }
}

internal static class Timestamp
{
    public const string OidTimestampingEku = "1.3.6.1.5.5.7.3.8";

    /// <summary>
    /// Extracts the RFC 3161 timestamp token from a signer's unsigned attributes,
    /// verifies that it stamps <paramref name="signatureValue"/> and returns its generation time.
    /// </summary>
    /// <summary>OID of the countersignature unsigned attribute.</summary>
    public const string OidCounterSignature = "1.2.840.113549.1.9.6";

    /// <summary>
    /// Reads a timestamp attached to <paramref name="signer"/>. VS Code extension
    /// signatures in the wild carry either an RFC 3161 unsigned timestamp attribute
    /// or a countersignature whose own unsigned attributes contain the timestamp.
    /// </summary>
    public static TimestampInfo? Read(SignerInfo signer)
    {
        var direct = ReadTimestampAttribute(signer, signer.Signature);
        if (direct is not null) return direct;

        var countersignature = signer.FindUnsignedAttribute(OidCounterSignature);
        if (countersignature is null) return null;

        TimestampInfo? result = null;
        foreach (var value in countersignature.Values)
        {
            SignerInfo inner;
            try
            {
                inner = SignedData.ParseSignerInfo(value);
            }
            catch (Exception ex) when (ex is AsnContentException or CryptographicException)
            {
                continue;
            }
            var nested = ReadTimestampAttribute(inner, signer.Signature);
            if (nested is null) continue;
            if (nested.Check == TimestampCheck.Ok) return nested;
            result ??= nested;
        }
        return result;
    }

    private static TimestampInfo? ReadTimestampAttribute(SignerInfo signer, byte[] stampedValue)
    {
        var attr = signer.FindUnsignedAttribute(Der.OidTimeStampToken);
        if (attr is null || attr.Values.Count == 0) return null;

        SignedData token;
        try
        {
            token = SignedData.Parse(attr.Values[0]);
        }
        catch (Exception ex) when (ex is AsnContentException or CryptographicException)
        {
            return new TimestampInfo { Check = TimestampCheck.Malformed, Detail = ex.Message };
        }

        TimestampInfo Fail(TimestampCheck check, string detail, DateTimeOffset? genTime = null, X509Certificate2? signer = null)
        {
            var failure = new TimestampInfo { Check = check, Detail = detail, Token = token, GenTime = genTime, SignerCertificate = signer };
            failure.Certificates.AddRange(token.Certificates);
            return failure;
        }

        if (token.EncapsulatedContentType != Der.OidTstInfo || token.Content is null)
            return Fail(TimestampCheck.Malformed, "the timestamp token does not contain a TSTInfo structure");

        // The message imprint is a hash over the value the token stamps: the
        // signature value of the primary signature for a direct timestamp, or the
        // countersigned value for a timestamp nested in a countersignature.
        var imprint = SHA256.HashData(stampedValue);
        if (token.Content.Length < 32 || !token.Content.AsSpan(0, 32).SequenceEqual(imprint))
        {
            // Fall back to a structural parse so that the failure is precise.
            var parsed = TstInfo.TryParse(token.Content);
            var matched = parsed is not null && parsed.Value.Imprint.SequenceEqual(imprint);
            if (!matched)
                return Fail(TimestampCheck.ImprintMismatch, "the timestamp message imprint does not match the signature value");
        }

        if (token.SignerInfos.Count != 1)
            return Fail(TimestampCheck.Malformed, $"the timestamp token has {token.SignerInfos.Count} signers");

        var tsSigner = token.SignerInfos[0];
        if (tsSigner.Certificate is null)
            return Fail(TimestampCheck.Malformed, "the timestamp signer certificate is missing");

        var result = CmsSignature.Verify(token, tsSigner, token.Content, detached: false, out var detail);
        if (result != SignatureCheckResult.Ok)
            return Fail(TimestampCheck.SignatureInvalid, detail, signer: tsSigner.Certificate);

        var parsedInfo = TstInfo.TryParse(token.Content);
        if (parsedInfo is null)
            return Fail(TimestampCheck.Malformed, "the timestamp token TSTInfo is unreadable");

        if (!HasEku(tsSigner.Certificate, OidTimestampingEku))
            return Fail(TimestampCheck.InvalidEku, "the timestamp signer certificate lacks the timestamping EKU", parsedInfo.Value.GenTime, tsSigner.Certificate);

        var success = new TimestampInfo
        {
            Check = TimestampCheck.Ok,
            Detail = "the signature is timestamped",
            GenTime = parsedInfo.Value.GenTime,
            SignerCertificate = tsSigner.Certificate,
            Token = token,
        };
        success.Certificates.AddRange(token.Certificates);
        return success;
    }

    public static bool HasEku(X509Certificate2 certificate, string oid)
    {
        foreach (var ext in certificate.Extensions)
        {
            if (ext is X509EnhancedKeyUsageExtension eku)
                foreach (var o in eku.EnhancedKeyUsages)
                    if (o.Value == oid) return true;
        }
        return false;
    }
}

internal readonly struct TstInfo
{
    public DateTimeOffset GenTime { get; init; }
    public byte[] Imprint { get; init; }

    public static TstInfo? TryParse(byte[] der)
    {
        try
        {
            var outer = Der.Sequence(der);
            outer.ReadInteger(); // version
            outer.ReadObjectIdentifier(); // policy
            var imprintSeq = outer.ReadSequence();
            Der.ReadAlgorithmIdentifier(ref imprintSeq); // hashAlgorithm
            var imprint = imprintSeq.ReadOctetString();
            imprintSeq.ThrowIfNotEmpty();
            outer.ReadInteger(); // serialNumber
            var genTime = Der.ReadTime(ref outer);
            return new TstInfo { GenTime = genTime, Imprint = imprint };
        }
        catch (Exception ex) when (ex is AsnContentException or CryptographicException)
        {
            return null;
        }
    }
}
