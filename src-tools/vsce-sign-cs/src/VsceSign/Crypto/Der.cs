using System.Formats.Asn1;

namespace VsceSign.Crypto;

/// <summary>
/// Small helpers over <see cref="AsnReader"/> for the DER structures we need to
/// walk (CMS SignedData, X.509, CRL, OCSP basic response).
/// </summary>
internal static class Der
{
    public const string OidSignedData = "1.2.840.113549.1.7.2";
    public const string OidData = "1.2.840.113549.1.7.1";
    public const string OidTstInfo = "1.2.840.113549.1.9.16.1.4";
    public const string OidTimeStampToken = "1.2.840.113549.1.9.16.2.14";
    public const string OidMessageDigest = "1.2.840.113549.1.9.4";
    public const string OidContentType = "1.2.840.113549.1.9.3";
    public const string OidSigningCertificateV2 = "1.2.840.113549.1.9.16.2.47";
    public const string OidSigningCertificate = "1.2.840.113549.1.9.16.2.12";
    public const string OidCommitmentType = "1.2.840.113549.1.9.16.2.16";
    public const string OidProofOfReceipt = "1.2.840.113549.1.9.16.6.1";

    public const string OidSha256 = "2.16.840.1.101.3.4.2.1";
    public const string OidSha384 = "2.16.840.1.101.3.4.2.2";
    public const string OidSha512 = "2.16.840.1.101.3.4.2.3";
    public const string OidSha1 = "1.3.14.3.2.26";
    public const string OidMd5 = "1.2.840.113549.2.5";

    public const string OidRsaEncryption = "1.2.840.113549.1.1.1";
    public const string OidSha256WithRsa = "1.2.840.113549.1.1.11";
    public const string OidSha384WithRsa = "1.2.840.113549.1.1.12";
    public const string OidSha512WithRsa = "1.2.840.113549.1.1.13";
    public const string OidSha1WithRsa = "1.2.840.113549.1.1.5";
    public const string OidEcPublicKey = "1.2.840.10045.2.1";
    public const string OidEcdsaWithSha256 = "1.2.840.10045.4.3.2";
    public const string OidEcdsaWithSha384 = "1.2.840.10045.4.3.3";
    public const string OidEcdsaWithSha512 = "1.2.840.10045.4.3.4";

    public static Asn1Tag ContextConstructed(int tag) => new(TagClass.ContextSpecific, tag, isConstructed: true);

    public static Asn1Tag ContextPrimitive(int tag) => new(TagClass.ContextSpecific, tag, isConstructed: false);

    /// <summary>
    /// Returns a reader over the *contents* of a SEQUENCE. BER is used because
    /// real world CMS structures are frequently encoded with BER (and Microsoft's
    /// own signatures do not sort every SET OF).
    /// </summary>
    public static AsnReader Sequence(ReadOnlyMemory<byte> der) =>
        new AsnReader(der, AsnEncodingRules.BER).ReadSequence();

    /// <summary>
    /// Reads a SET OF without enforcing DER sort order: the certificates and signer
    /// infos produced by Microsoft's signer are not sorted.
    /// </summary>
    public static AsnReader SetOf(ref AsnReader reader, Asn1Tag? tag = null) =>
        tag is null ? reader.ReadSetOf(skipSortOrderValidation: true) : reader.ReadSetOf(skipSortOrderValidation: true, tag);

    /// <summary>Reads a single ASN.1 value that is an OBJECT IDENTIFIER.</summary>
    public static string ReadOidValue(ReadOnlyMemory<byte> der) =>
        new AsnReader(der, AsnEncodingRules.BER).ReadObjectIdentifier();

    /// <summary>Reads a single ASN.1 value that is an OCTET STRING.</summary>
    public static byte[] ReadOctetStringValue(ReadOnlyMemory<byte> der) =>
        new AsnReader(der, AsnEncodingRules.BER).ReadOctetString().ToArray();

    /// <summary>Reads the value bytes of a DER INTEGER.</summary>
    public static byte[] IntegerBytes(ReadOnlyMemory<byte> der) =>
        new AsnReader(der, AsnEncodingRules.DER).ReadIntegerBytes().ToArray();

    public static string ReadOid(ref AsnReader reader) =>
        reader.ReadObjectIdentifier(Asn1Tag.ObjectIdentifier);

    /// <summary>Reads an AlgorithmIdentifier and returns (oid, parameters-present).</summary>
    public static string ReadAlgorithmIdentifier(ref AsnReader reader)
    {
        var seq = reader.ReadSequence();
        var oid = seq.ReadObjectIdentifier();
        if (seq.HasData) seq.ReadEncodedValue();
        seq.ThrowIfNotEmpty();
        return oid;
    }

    public static byte[] ReadOctetString(ref AsnReader reader) =>
        reader.ReadOctetString();

    /// <summary>Peels nested OCTET STRING wrappers (used by OCSP extensions).</summary>
    public static byte[] UnwrapOctetString(byte[] data)
    {
        try
        {
            var r = Sequence(data);
            if (r.PeekTag().HasSameClassAndValue(Asn1Tag.PrimitiveOctetString))
            {
                var inner = r.ReadOctetString();
                r.ThrowIfNotEmpty();
                return inner;
            }
        }
        catch (AsnContentException)
        {
        }
        return data;
    }

    /// <summary>Removes DER INTEGER sign padding so serial numbers compare equal.</summary>
    public static byte[] TrimLeadingZeros(byte[] value)
    {
        int i = 0;
        while (i < value.Length - 1 && value[i] == 0) i++;
        return i == 0 ? value : value[i..];
    }

    public static DateTimeOffset ReadTime(ref AsnReader reader)
    {
        var tag = reader.PeekTag();
        if (tag.HasSameClassAndValue(Asn1Tag.UtcTime)) return reader.ReadUtcTime();
        if (tag.HasSameClassAndValue(Asn1Tag.GeneralizedTime)) return reader.ReadGeneralizedTime();
        throw new AsnContentException("Expected UTCTime or GeneralizedTime.");
    }
}
