using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace VsceSign.Crypto;

internal enum SignatureCheckResult
{
    Ok,
    /// <summary>The cryptographic signature over the signed attributes/content is invalid.</summary>
    Invalid,
    /// <summary>The signature algorithm or key type is not supported.</summary>
    Unsupported,
    /// <summary>The message digest attribute does not match the content.</summary>
    DigestMismatch,
    /// <summary>The content type attribute is wrong.</summary>
    ContentTypeMismatch,
    /// <summary>No signer certificate could be resolved.</summary>
    NoCertificate,
}

internal static class CmsSignature
{
    private static readonly Dictionary<string, HashAlgorithmName> DigestFromOid = new()
    {
        [Der.OidSha256] = HashAlgorithmName.SHA256,
        [Der.OidSha384] = HashAlgorithmName.SHA384,
        [Der.OidSha512] = HashAlgorithmName.SHA512,
        [Der.OidSha1] = HashAlgorithmName.SHA1,
    };

    private static readonly Dictionary<string, HashAlgorithmName> DigestFromSignatureOid = new()
    {
        [Der.OidSha256WithRsa] = HashAlgorithmName.SHA256,
        [Der.OidSha384WithRsa] = HashAlgorithmName.SHA384,
        [Der.OidSha512WithRsa] = HashAlgorithmName.SHA512,
        [Der.OidSha1WithRsa] = HashAlgorithmName.SHA1,
        [Der.OidEcdsaWithSha256] = HashAlgorithmName.SHA256,
        [Der.OidEcdsaWithSha384] = HashAlgorithmName.SHA384,
        [Der.OidEcdsaWithSha512] = HashAlgorithmName.SHA512,
    };

    public static HashAlgorithmName? GetHashAlgorithm(string digestOid) =>
        DigestFromOid.TryGetValue(digestOid, out var h) ? h : null;

    /// <summary>
    /// Verifies a CMS signer info against <paramref name="content"/> (the signed
    /// content; for detached signatures the caller supplies it).
    /// </summary>
    public static SignatureCheckResult Verify(
        SignedData signedData,
        SignerInfo signer,
        ReadOnlyMemory<byte> content,
        bool detached,
        out string detail)
    {
        detail = string.Empty;
        var signerCert = signer.Certificate;
        if (signerCert is null)
        {
            detail = "the signer certificate is not present in the signature";
            return SignatureCheckResult.NoCertificate;
        }

        var digestAlgorithm = GetHashAlgorithm(signer.DigestAlgorithmOid);
        if (digestAlgorithm is null)
        {
            detail = $"unsupported digest algorithm {signer.DigestAlgorithmOid}";
            return SignatureCheckResult.Unsupported;
        }

        HashAlgorithmName? signatureDigest = null;
        bool isRsa = signer.SignatureAlgorithmOid == Der.OidRsaEncryption;
        bool isEcdsa = signer.SignatureAlgorithmOid is Der.OidEcdsaWithSha256 or Der.OidEcdsaWithSha384 or Der.OidEcdsaWithSha512;
        if (isRsa)
        {
            signatureDigest = digestAlgorithm;
        }
        else if (isEcdsa)
        {
            if (!DigestFromSignatureOid.TryGetValue(signer.SignatureAlgorithmOid, out var fromSig) ||
                fromSig != digestAlgorithm)
            {
                detail = $"digest algorithm {signer.DigestAlgorithmOid} does not match signature algorithm {signer.SignatureAlgorithmOid}";
                return SignatureCheckResult.Unsupported;
            }
            signatureDigest = fromSig;
        }
        else if (DigestFromSignatureOid.TryGetValue(signer.SignatureAlgorithmOid, out var v))
        {
            signatureDigest = v;
            isRsa = signer.SignatureAlgorithmOid is Der.OidSha256WithRsa or Der.OidSha384WithRsa or Der.OidSha512WithRsa or Der.OidSha1WithRsa;
        }
        else
        {
            detail = $"unsupported signature algorithm {signer.SignatureAlgorithmOid}";
            return SignatureCheckResult.Unsupported;
        }

        if (signer.SignedAttributesSetDer is null)
        {
            // No signed attributes: the signature is over the content itself.
            if (detached && content.IsEmpty)
            {
                detail = "the signature has no signed attributes and no content was supplied";
                return SignatureCheckResult.Invalid;
            }
            return VerifyRaw(signerCert, content, signer.Signature, signatureDigest.Value, isRsa, out detail);
        }

        // Signed attributes: verify the messageDigest / contentType attributes first,
        // then verify the signature over the DER SET OF attributes.
        var contentTypeAttr = signer.FindSignedAttribute(Der.OidContentType);
        if (contentTypeAttr is not null && contentTypeAttr.Values.Count > 0)
        {
            var oid = Der.ReadOidValue(contentTypeAttr.Values[0]);
            if (oid != signedData.EncapsulatedContentType)
            {
                detail = $"the contentType attribute ({oid}) does not match the encapsulated content type ({signedData.EncapsulatedContentType})";
                return SignatureCheckResult.ContentTypeMismatch;
            }
        }

        var messageDigestAttr = signer.FindSignedAttribute(Der.OidMessageDigest);
        if (messageDigestAttr is null || messageDigestAttr.Values.Count == 0)
        {
            detail = "the signature is missing the messageDigest attribute";
            return SignatureCheckResult.Invalid;
        }

        var expected = Der.ReadOctetStringValue(messageDigestAttr.Values[0]);
        var actual = Hash(content, digestAlgorithm.Value);
        if (!CryptographicOperations.FixedTimeEquals(expected, actual))
        {
            detail = "the messageDigest attribute does not match the signed content";
            return SignatureCheckResult.DigestMismatch;
        }

        return VerifyRaw(signerCert, signer.SignedAttributesSetDer, signer.Signature, signatureDigest.Value, isRsa, out detail);
    }

    private static SignatureCheckResult VerifyRaw(
        X509Certificate2 certificate,
        ReadOnlyMemory<byte> data,
        byte[] signature,
        HashAlgorithmName hashAlgorithm,
        bool isRsa,
        out string detail)
    {
        detail = string.Empty;
        try
        {
            if (isRsa)
            {
                using var rsa = certificate.GetRSAPublicKey();
                if (rsa is null)
                {
                    detail = "the signer certificate does not contain an RSA public key";
                    return SignatureCheckResult.Unsupported;
                }
                if (rsa.VerifyData(data.Span, signature, hashAlgorithm, RSASignaturePadding.Pkcs1))
                    return SignatureCheckResult.Ok;
                detail = $"the RSA signature over {data.Length} bytes does not verify with {hashAlgorithm.Name} (PKCS#1 v1.5)";
                return SignatureCheckResult.Invalid;
            }

            using var ecdsa = certificate.GetECDsaPublicKey();
            if (ecdsa is null)
            {
                detail = "the signer certificate does not contain an ECDSA public key";
                return SignatureCheckResult.Unsupported;
            }
            return ecdsa.VerifyData(data.Span, signature, hashAlgorithm, DSASignatureFormat.Rfc3279DerSequence)
                ? SignatureCheckResult.Ok
                : SignatureCheckResult.Invalid;
        }
        catch (CryptographicException ex)
        {
            detail = ex.Message;
            return SignatureCheckResult.Invalid;
        }
    }

    public static byte[] Hash(ReadOnlyMemory<byte> data, HashAlgorithmName algorithm)
    {
        if (algorithm == HashAlgorithmName.SHA256) return SHA256.HashData(data.Span);
        if (algorithm == HashAlgorithmName.SHA384) return SHA384.HashData(data.Span);
        if (algorithm == HashAlgorithmName.SHA512) return SHA512.HashData(data.Span);
        if (algorithm == HashAlgorithmName.SHA1) return SHA1.HashData(data.Span);
        throw new NotSupportedException($"Unsupported hash algorithm {algorithm}.");
    }
}
