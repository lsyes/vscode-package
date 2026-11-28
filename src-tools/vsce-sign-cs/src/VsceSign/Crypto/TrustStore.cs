using System.Reflection;
using System.Security.Cryptography.X509Certificates;

namespace VsceSign.Crypto;

/// <summary>
/// The trust model of Microsoft's vsce-sign, reproduced.
///
/// Every case that reaches this class uses the same chain building call: build
/// the chain of the certificate being evaluated at the effective time, using the
/// certificates carried in the signature as untrusted intermediates, and
/// terminating in one of the trust anchors below. All other Microsoft vsce-sign
/// policies are checks on top of that single chain build:
///
///   * vsmp    - the primary signature must be a Visual Studio Marketplace
///               publisher signature or carry a Visual Studio Marketplace
///               repository countersignature, and both the publisher and the
///               repository certificate must chain to a trust anchor;
///   * signing - the signing certificate must chain to a trust anchor and carry
///               the code signing EKU;
///   * "TrustedSignerPolicy failed. The primary signature is not a Microsoft
///     publisher." is emitted when the *root* of the signing certificate's
///     chain is not a Microsoft root - i.e. it is another application of the
///     same chain build, evaluated against the same anchor set.
///
/// Trust anchors therefore come from one bundle, not two.
/// </summary>
internal static class TrustStore
{
    /// <summary>
    /// Loads the trust anchors embedded as <c>Resources/trusted-roots.pem</c>,
    /// which is a byte-for-byte copy of the
    /// <c>CodeSigningFallbackCertificateBundle</c> resource compiled into the
    /// Microsoft vsce-sign 2.0.8 NativeAOT binary, plus the certificate
    /// resources (<c>Resources/publisher-certs.pem</c>) that the same binary
    /// carries under Microsoft.VisualStudio.Extensions.Signing.Resources.Certificates.
    /// </summary>
    public const string ExtraAnchorsVariable = "VSCE_SIGN_EXTRA_TRUST_ANCHORS";

    public static (X509Certificate2Collection Anchors, int BundleCount, int PublisherPinCount) Load()
    {
        var anchors = new X509Certificate2Collection();
        int bundle = 0, pins = 0;

        // Escape hatch: colon separated PEM files whose certificates are added to the
        // anchor set. Empty by default, so the trust model is exactly Microsoft's
        // unless an operator deliberately extends it.
        foreach (var path in (Environment.GetEnvironmentVariable(ExtraAnchorsVariable) ?? string.Empty)
                     .Split(':', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            if (!File.Exists(path)) continue;
            foreach (var der in PemCerts(File.ReadAllText(path)))
                Add(anchors, der);
        }

        foreach (var pem in ReadResource("trusted-roots.pem"))
        {
            foreach (var der in PemCerts(pem))
            {
                if (Add(anchors, der)) bundle++;
            }
        }

        foreach (var pem in ReadResource("publisher-certs.pem"))
        {
            foreach (var der in PemCerts(pem))
            {
                // The bundled publisher certificates are self-contained trust hints
                // for the Marketplace publisher identities; they are only added to
                // the anchor set when they are self-signed (roots).
                var cert = X509CertificateLoader.LoadCertificate(der);
                if (cert.Subject.Equals(cert.Issuer, StringComparison.Ordinal))
                {
                    if (Add(anchors, der)) pins++;
                }
            }
        }

        return (anchors, bundle, pins);
    }

    private static bool Add(X509Certificate2Collection anchors, byte[] der)
    {
        foreach (var existing in anchors)
            if (existing.RawData.AsSpan().SequenceEqual(der)) return false;
        anchors.Add(X509CertificateLoader.LoadCertificate(der));
        return true;
    }

    private static List<byte[]> PemCerts(string pem)
    {
        var result = new List<byte[]>();
        const string begin = "-----BEGIN CERTIFICATE-----";
        const string end = "-----END CERTIFICATE-----";
        int index = 0;
        while (true)
        {
            int start = pem.IndexOf(begin, index, StringComparison.Ordinal);
            if (start < 0) break;
            int stop = pem.IndexOf(end, start, StringComparison.Ordinal);
            if (stop < 0) break;
            var body = pem[(start + begin.Length)..stop];
            var bytes = Convert.FromBase64String(string.Concat(body.Where(c => !char.IsWhiteSpace(c))));
            result.Add(bytes);
            index = stop + end.Length;
        }
        return result;
    }

    private static List<string> ReadResource(string name)
    {
        var assembly = Assembly.GetExecutingAssembly();
        var resourceName = assembly.GetManifestResourceNames()
            .FirstOrDefault(n => n.EndsWith("." + name, StringComparison.Ordinal))
            ?? throw new InvalidOperationException($"Embedded resource {name} is missing.");
        using var stream = assembly.GetManifestResourceStream(resourceName)!;
        using var reader = new StreamReader(stream);
        return new List<string> { reader.ReadToEnd() };
    }
}
