using System.Formats.Asn1;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace VsceSign.Crypto;

internal sealed class SignerInfo
{
    public required byte[] IssuerDer { get; init; }
    public required byte[] SerialDer { get; init; }
    public byte[]? SubjectKeyIdentifier { get; init; }
    public required string DigestAlgorithmOid { get; init; }
    /// <summary>DER of the signed attributes re-tagged as an explicit SET OF (0x31), or null.</summary>
    public byte[]? SignedAttributesSetDer { get; init; }
    /// <summary>Parsed signed attributes (content type, message digest, signing time, ...).</summary>
    public List<CmsAttribute> SignedAttributes { get; } = new();
    public required string SignatureAlgorithmOid { get; init; }
    public required byte[] Signature { get; init; }
    public List<CmsAttribute> UnsignedAttributes { get; } = new();
    /// <summary>The certificate the signer info refers to, resolved after construction.</summary>
    public X509Certificate2? Certificate { get; set; }

    public CmsAttribute? FindSignedAttribute(string oid) =>
        SignedAttributes.FirstOrDefault(a => a.Oid == oid);

    public CmsAttribute? FindUnsignedAttribute(string oid) =>
        UnsignedAttributes.FirstOrDefault(a => a.Oid == oid);
}

internal sealed class CmsAttribute
{
    public required string Oid { get; init; }
    /// <summary>Raw DER (including the header) of each attribute value.</summary>
    public List<byte[]> Values { get; } = new();
}

internal sealed class SignedData
{
    public required string EncapsulatedContentType { get; init; }
    /// <summary>Null for detached signatures.</summary>
    public byte[]? Content { get; init; }
    public List<X509Certificate2> Certificates { get; } = new();
    public List<SignerInfo> SignerInfos { get; } = new();

    /// <summary>
    /// Parses a BER/DER CMS SignedData structure. <paramref name="detachedContent"/>
    /// supplies the content for detached signatures; when null the encapsulated
    /// content (if any) is used.
    /// </summary>
    public static SignedData Parse(ReadOnlyMemory<byte> cmsDer, ReadOnlyMemory<byte>? detachedContent = null)
    {
        var outer = Der.Sequence(cmsDer);
        var contentType = outer.ReadObjectIdentifier();
        if (contentType != Der.OidSignedData)
            throw new AsnContentException($"Not a CMS SignedData (content type {contentType}).");

        var explicitContent = outer.ReadSequence(Der.ContextConstructed(0));
        var sd = explicitContent.ReadSequence();
        explicitContent.ThrowIfNotEmpty();

        _ = sd.ReadInteger(); // version
        var digestAlgorithms = Der.SetOf(ref sd);
        while (digestAlgorithms.HasData) digestAlgorithms.ReadEncodedValue();

        // EncapsulatedContentInfo
        var eci = sd.ReadSequence();
        var eContentType = eci.ReadObjectIdentifier();
        byte[]? content = null;
        if (eci.HasData)
        {
            var octets = eci.ReadOctetString(Der.ContextConstructed(0));
            content = octets;
        }
        eci.ThrowIfNotEmpty();
        if (content is null && detachedContent is not null) content = detachedContent.Value.ToArray();
        if (content is null && detachedContent is null)
        {
            // Detached signature with no content supplied -> content stays null.
        }

        var result = new SignedData { EncapsulatedContentType = eContentType, Content = content };

        // certificates [0] IMPLICIT CertificateSet OPTIONAL
        if (sd.HasData && sd.PeekTag().HasSameClassAndValue(Der.ContextConstructed(0)))
        {
            var certSet = Der.SetOf(ref sd, Der.ContextConstructed(0));
            while (certSet.HasData)
            {
                var tag = certSet.PeekTag();
                if (tag.HasSameClassAndValue(new Asn1Tag(TagClass.ContextSpecific, 0, true)) ||
                    tag.HasSameClassAndValue(new Asn1Tag(TagClass.ContextSpecific, 1, true)) ||
                    tag.HasSameClassAndValue(new Asn1Tag(TagClass.ContextSpecific, 2, true)) ||
                    tag.HasSameClassAndValue(new Asn1Tag(TagClass.ContextSpecific, 3, true)))
                {
                    // other certificate formats - skip
                    certSet.ReadEncodedValue();
                    continue;
                }
                var raw = certSet.ReadEncodedValue();
                try
                {
                    result.Certificates.Add(X509CertificateLoader.LoadCertificate(raw.ToArray()));
                }
                catch (CryptographicException)
                {
                    // Ignore malformed / unsupported certificates; the caller reports untrusted.
                }
            }
        }

        // crls [1] IMPLICIT RevocationInfoChoices OPTIONAL. Revocation information is
        // fetched from the CRL distribution points instead, so the set is skipped.
        if (sd.HasData && sd.PeekTag().HasSameClassAndValue(Der.ContextConstructed(1)))
        {
            var crlSet = Der.SetOf(ref sd, Der.ContextConstructed(1));
            while (crlSet.HasData) crlSet.ReadEncodedValue();
        }

        var signerInfos = Der.SetOf(ref sd);
        while (signerInfos.HasData)
            result.SignerInfos.Add(ReadSignerInfo(ref signerInfos));

        sd.ThrowIfNotEmpty();

        foreach (var si in result.SignerInfos)
            si.Certificate = FindCertificate(result.Certificates, si);

        return result;
    }

    /// <summary>Parses a bare SignerInfo (used for repository countersignatures).</summary>
    public static SignerInfo ParseSignerInfo(ReadOnlyMemory<byte> der)
    {
        var reader = new AsnReader(der, AsnEncodingRules.BER);
        return ReadSignerInfo(ref reader);
    }

    public static byte[] SerialOf(SignerInfo signer) =>
        signer.SerialDer.Length == 0 ? Array.Empty<byte>() : Der.TrimLeadingZeros(Der.IntegerBytes(signer.SerialDer));

    private static SignerInfo ReadSignerInfo(ref AsnReader reader)
    {
        var si = reader.ReadSequence();
        _ = si.ReadInteger(); // version

        byte[]? issuerDer = null, serialDer = null, ski = null;
        var sidTag = si.PeekTag();
        if (sidTag.HasSameClassAndValue(Der.ContextPrimitive(0)))
        {
            ski = si.ReadOctetString(Der.ContextPrimitive(0));
        }
        else
        {
            var ias = si.ReadSequence();
            issuerDer = ias.ReadEncodedValue().ToArray();
            serialDer = ias.ReadEncodedValue().ToArray();
            ias.ThrowIfNotEmpty();
        }

        var digestOid = Der.ReadAlgorithmIdentifier(ref si);

        byte[]? signedAttrsSet = null;
        var attrs = new List<CmsAttribute>();
        if (si.HasData && si.PeekTag().HasSameClassAndValue(Der.ContextConstructed(0)))
        {
            // The signature covers the DER encoding of the attributes as a SET OF:
            // the implicit [0] tag must be re-tagged to 0x31.
            var implicitTagged = si.ReadEncodedValue().ToArray();
            signedAttrsSet = RetagToSet(implicitTagged);
            attrs = ReadAttributes(signedAttrsSet);
        }

        var sigAlg = Der.ReadAlgorithmIdentifier(ref si);
        var signature = si.ReadOctetString();

        var unsigned = new List<CmsAttribute>();
        if (si.HasData && si.PeekTag().HasSameClassAndValue(Der.ContextConstructed(1)))
        {
            var implicitTagged = si.ReadEncodedValue().ToArray();
            unsigned = ReadAttributes(RetagToSet(implicitTagged));
        }

        si.ThrowIfNotEmpty();

        var result = new SignerInfo
        {
            IssuerDer = issuerDer ?? Array.Empty<byte>(),
            SerialDer = serialDer ?? Array.Empty<byte>(),
            SubjectKeyIdentifier = ski,
            DigestAlgorithmOid = digestOid,
            SignedAttributesSetDer = signedAttrsSet,
            SignatureAlgorithmOid = sigAlg,
            Signature = signature,
        };
        result.SignedAttributes.AddRange(attrs);
        result.UnsignedAttributes.AddRange(unsigned);
        return result;
    }

    /// <summary>
    /// Converts an implicitly tagged SET OF ([0] or [1], tag 0xA0/0xA1) into the
    /// canonical DER encoding of the equivalent SET OF (tag 0x31) that the
    /// signature actually covers.
    ///
    /// The encoding Microsoft's signer emits is *not* DER sorted, but the RSA
    /// signature is computed over the canonical form: the attribute encodings must
    /// be sorted in ascending octet order before the digest is taken. .NET's own
    /// SignedCms behaves the same way, which is why a naive byte-for-byte re-tag
    /// (0xA0 -> 0x31) fails to verify Microsoft signatures.
    /// </summary>
    private static byte[] RetagToSet(byte[] implicitTagged)
    {
        var span = (ReadOnlySpan<byte>)implicitTagged;
        var (contentOffset, contentLength) = ReadHeader(span);
        var content = span[contentOffset..(contentOffset + contentLength)];

        var elements = new List<byte[]>();
        int offset = 0;
        while (offset < content.Length)
        {
            var (childOffset, childLength) = ReadHeader(content[offset..]);
            int elementLength = childOffset + childLength;
            elements.Add(content.Slice(offset, elementLength).ToArray());
            offset += elementLength;
        }

        // DER SET OF: ascending by encoded octets.
        elements.Sort(static (x, y) => ((ReadOnlySpan<byte>)x).SequenceCompareTo(y));

        int totalLength = 0;
        foreach (var element in elements) totalLength += element.Length;

        var result = new byte[HeaderLengthFor(totalLength) + totalLength];
        int written = WriteHeader(result, 0x31, totalLength);
        foreach (var element in elements)
        {
            element.CopyTo(result, written);
            written += element.Length;
        }
        return result;
    }

    private static (int ContentOffset, int ContentLength) ReadHeader(ReadOnlySpan<byte> data)
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

    private static int HeaderLengthFor(int contentLength) =>
        contentLength < 0x80 ? 2 :
        contentLength <= 0xff ? 3 :
        contentLength <= 0xffff ? 4 : 5;

    private static int WriteHeader(byte[] buffer, byte tag, int contentLength)
    {
        buffer[0] = tag;
        if (contentLength < 0x80)
        {
            buffer[1] = (byte)contentLength;
            return 2;
        }
        if (contentLength <= 0xff)
        {
            buffer[1] = 0x81;
            buffer[2] = (byte)contentLength;
            return 3;
        }
        if (contentLength <= 0xffff)
        {
            buffer[1] = 0x82;
            buffer[2] = (byte)(contentLength >> 8);
            buffer[3] = (byte)contentLength;
            return 4;
        }
        buffer[1] = 0x83;
        buffer[2] = (byte)(contentLength >> 16);
        buffer[3] = (byte)(contentLength >> 8);
        buffer[4] = (byte)contentLength;
        return 5;
    }

    private static List<CmsAttribute> ReadAttributes(byte[] setDer)
    {
        var result = new List<CmsAttribute>();
        var set = new AsnReader(setDer, AsnEncodingRules.BER).ReadSetOf(skipSortOrderValidation: true);
        while (set.HasData)
        {
            var attrSeq = set.ReadSequence();
            var oid = attrSeq.ReadObjectIdentifier();
            var attr = new CmsAttribute { Oid = oid };
            var values = Der.SetOf(ref attrSeq);
            while (values.HasData) attr.Values.Add(values.ReadEncodedValue().ToArray());
            attrSeq.ThrowIfNotEmpty();
            result.Add(attr);
        }
        return result;
    }

    private static X509Certificate2? FindCertificate(List<X509Certificate2> certs, SignerInfo si)
    {
        foreach (var c in certs)
        {
            if (si.SubjectKeyIdentifier is not null)
            {
                var ext = c.Extensions["2.5.29.14"];
                if (ext is null) continue;
                byte[] inner;
                try
                {
                    inner = new AsnReader(ext.RawData, AsnEncodingRules.BER).ReadOctetString();
                }
                catch (AsnContentException)
                {
                    inner = ext.RawData;
                }
                if (inner.AsSpan().SequenceEqual(si.SubjectKeyIdentifier)) return c;
            }
            else
            {
                if (si.IssuerDer.Length == 0) continue;
                if (c.IssuerName.RawData.AsSpan().SequenceEqual(si.IssuerDer) &&
                    c.SerialNumberBytes.Span.SequenceEqual(SerialOf(si)))
                {
                    return c;
                }
            }
        }
        return null;
    }

}
