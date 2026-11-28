using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace VsceSign.Crypto;

internal enum TrustFailure
{
    None,
    /// <summary>The chain could not be built (missing, expired, not-yet-valid, bad signature, ...).</summary>
    ChainBuildFailed,
    /// <summary>The chain terminated in a self-signed certificate that is not a configured trust anchor.</summary>
    UntrustedRoot,
    /// <summary>A certificate in the chain is outside of its validity period at the effective time.</summary>
    Expired,
    /// <summary>A certificate in the chain has been revoked.</summary>
    Revoked,
}

internal sealed class ChainResult
{
    public TrustFailure Failure { get; init; }
    public string Detail { get; init; } = string.Empty;
    public List<X509Certificate2> Chain { get; init; } = new();
    public bool Trusted => Failure == TrustFailure.None;

    /// <summary>The chain built at the effective time, used as the input for revocation checks.</summary>
    public X509Chain? BuiltChain { get; init; }

    public static ChainResult Ok(List<X509Certificate2> chain, X509Chain builtChain) =>
        new() { Failure = TrustFailure.None, Chain = chain, BuiltChain = builtChain, Detail = "the certificate chain is trusted" };

    public static ChainResult Fail(TrustFailure failure, string detail) =>
        new() { Failure = failure, Detail = detail };
}

/// <summary>
/// Builds and validates X.509 chains against the trust anchors extracted from the
/// Microsoft vsce-sign binary. The caller passes the effective verification time
/// (the signing time for a publisher signature whose certificate has expired but
/// which carries a valid RFC 3161 timestamp).
/// </summary>
internal sealed class ChainValidator
{
    private readonly X509Certificate2Collection _anchors;

    public ChainValidator(X509Certificate2Collection anchors) => _anchors = anchors;

    public int AnchorCount => _anchors.Count;

    /// <summary>
    /// Builds the chain for <paramref name="certificate"/> using <paramref name="extraStore"/>
    /// (the certificates embedded in the signature) to fill in intermediates.
    /// </summary>
    public ChainResult Build(
        X509Certificate2 certificate,
        X509Certificate2Collection extraStore,
        DateTimeOffset effectiveTime)
    {
        try
        {
            // The chain is handed back to the caller for revocation checks, so it is
            // deliberately not disposed here.
            var chain = new X509Chain();
            chain.ChainPolicy.TrustMode = X509ChainTrustMode.CustomRootTrust;
            foreach (var anchor in _anchors) chain.ChainPolicy.CustomTrustStore.Add(anchor);
            foreach (var extra in extraStore) chain.ChainPolicy.ExtraStore.Add(extra);
            chain.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
            chain.ChainPolicy.VerificationTime = effectiveTime.UtcDateTime;
            chain.ChainPolicy.VerificationFlags = X509VerificationFlags.NoFlag;
            chain.ChainPolicy.DisableCertificateDownloads = true;

            var built = chain.Build(certificate);
            var elements = chain.ChainElements.Cast<X509ChainElement>().Select(e => e.Certificate).ToList();

            // Collect the status in a deterministic order of precedence.
            var failure = TrustFailure.None;
            var messages = new List<string>();
            foreach (var status in chain.ChainStatus)
            {
                switch (status.Status)
                {
                    case X509ChainStatusFlags.NoError:
                        break;
                    case X509ChainStatusFlags.UntrustedRoot:
                        if (failure < TrustFailure.UntrustedRoot) failure = TrustFailure.UntrustedRoot;
                        messages.Add($"{status.Status}: the root certificate is not trusted");
                        break;
                    case X509ChainStatusFlags.NotTimeValid:
                    case X509ChainStatusFlags.NotTimeNested:
                    case X509ChainStatusFlags.CtlNotTimeValid:
                        failure = TrustFailure.Expired;
                        messages.Add($"{status.Status}: a certificate in the chain is outside of its validity period");
                        break;
                    case X509ChainStatusFlags.Revoked:
                        if (failure < TrustFailure.Revoked) failure = TrustFailure.Revoked;
                        messages.Add($"{status.Status}: a certificate in the chain has been revoked");
                        break;
                    default:
                        if (failure == TrustFailure.None) failure = TrustFailure.ChainBuildFailed;
                        messages.Add($"{status.Status}: {status.StatusInformation?.Trim()}");
                        break;
                }
            }

            if (!built && failure == TrustFailure.None) failure = TrustFailure.ChainBuildFailed;

            // A chain may "build" successfully while terminating in a self-signed
            // certificate that is not one of our anchors (when Downloads are
            // disabled this does not normally happen, but be explicit).
            if (failure == TrustFailure.None && elements.Count > 0)
            {
                var root = elements[^1];
                bool anchored = _anchors.Cast<X509Certificate2>().Any(a =>
                    a.RawData.AsSpan().SequenceEqual(root.RawData));
                if (!anchored)
                {
                    failure = TrustFailure.UntrustedRoot;
                    messages.Add("the root certificate is not one of the configured trust anchors");
                }
            }

            if (failure != TrustFailure.None)
            {
                return new ChainResult
                {
                    Failure = failure,
                    Detail = string.Join("; ", messages.Distinct()),
                    Chain = elements,
                    BuiltChain = chain,
                };
            }

            return ChainResult.Ok(elements, chain);
        }
        catch (CryptographicException ex)
        {
            return ChainResult.Fail(TrustFailure.ChainBuildFailed, ex.Message);
        }
    }
}
