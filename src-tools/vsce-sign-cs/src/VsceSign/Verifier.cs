using System.IO.Compression;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using VsceSign.Crypto;

namespace VsceSign;

internal sealed class VerifierOptions
{
    public bool Verbose { get; init; }
    public TextWriter Diagnostics { get; init; } = Console.Error;
    /// <summary>Require a Visual Studio Marketplace publisher/repository signature.</summary>
    public bool RequireVisualStudioMarketplaceSignature { get; init; } = true;
    /// <summary>Reject extra entries in the package that the manifest does not list.</summary>
    public bool RequireNoExtraneousEntries { get; init; }
    /// <summary>Reject a publisher certificate that is outside of its validity period.</summary>
    public bool RequirePublisherCertificateTimeValid { get; init; }
    /// <summary>Fetch CRL/OCSP and fail with CertificateRevoked when a certificate is revoked.</summary>
    public bool RequireRevocationStatus { get; init; }
    /// <summary>Backwards-compatible alias for the revocation switch.</summary>
    public bool RequireRevocationCheck { get; init; }
    /// <summary>Require a repository countersignature rather than a marketplace publisher certificate.</summary>
    public bool RequireRepositoryCountersignature { get; init; }
}

/// <summary>
/// Verification of a signed VSIX package, mirroring Microsoft's
/// vsce-sign <c>verify --package &lt;vsix&gt; --signaturearchive &lt;zip&gt;</c>.
/// </summary>
internal sealed class Verifier
{
    private const string MarketplaceOrganizationalUnit = "Visual Studio Marketplace";
    private const string MarketplacePpeOrganizationalUnit = "Visual Studio Marketplace (PPE)";
    private const string CodeSigningEku = "1.3.6.1.5.5.7.3.3";
    private const string LifetimeSigningEku = "1.3.6.1.4.1.311.10.3.13";

    private readonly VerifierOptions _options;
    private readonly ChainValidator _chains;

    public Verifier(VerifierOptions options, ChainValidator chains)
    {
        _options = options;
        _chains = chains;
    }

    private void Log(string message)
    {
        if (_options.Verbose) _options.Diagnostics.WriteLine(message);
    }

    /// <summary>Reported whether or not --verbose was requested (failures).</summary>
    private void LogAlways(string message) => _options.Diagnostics.WriteLine(message);

    /// <summary>Reported only with --verbose.</summary>
    private void LogSuccess(string message)
    {
        if (_options.Verbose) _options.Diagnostics.WriteLine(message);
    }

    public ReturnCode Verify(string packagePath, string signatureArchivePath)
    {
        // ---- the package file must be readable ---------------------------------
        if (!File.Exists(packagePath))
        {
            LogAlways($"PackageIsUnreadable: the package file '{packagePath}' does not exist.");
            return ReturnCode.PackageIsUnreadable;
        }

        long packageSize;
        try
        {
            packageSize = ZipSupport.FileLength(packagePath);
            using var probe = new FileStream(packagePath, FileMode.Open, FileAccess.Read, FileShare.Read);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            LogAlways($"PackageIsUnreadable: {ex.Message}");
            return ReturnCode.PackageIsUnreadable;
        }
        Log($"Package file size:  {packageSize}");

        // ---- the signature archive must be readable ---------------------------
        if (!File.Exists(signatureArchivePath))
        {
            LogAlways($"SignatureArchiveIsUnreadable: the signature archive '{signatureArchivePath}' does not exist.");
            return ReturnCode.SignatureArchiveIsUnreadable;
        }

        long signatureArchiveSize;
        try
        {
            signatureArchiveSize = ZipSupport.FileLength(signatureArchivePath);
            using var probe = new FileStream(signatureArchivePath, FileMode.Open, FileAccess.Read, FileShare.Read);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            LogAlways($"SignatureArchiveIsUnreadable: {ex.Message}");
            return ReturnCode.SignatureArchiveIsUnreadable;
        }
        Log($"Signature archive file size:  {signatureArchiveSize}");

        // ---- the package must be a valid ZIP ---------------------------------
        ZipArchive package;
        try
        {
            package = ZipSupport.Open(packagePath, packageSize);
        }
        catch (ZipFormatException ex)
        {
            LogAlways($"PackageIsInvalidZip: {ex.Message}");
            return ReturnCode.PackageIsInvalidZip;
        }

        using (package)
        {
            // ---- the signature archive must be a valid ZIP -------------------
            ZipArchive archive;
            try
            {
                archive = ZipSupport.Open(signatureArchivePath, signatureArchiveSize);
            }
            catch (ZipFormatException ex)
            {
                LogAlways($"SignatureArchiveIsInvalidZip: {ex.Message}");
                return ReturnCode.SignatureArchiveIsInvalidZip;
            }

            using (archive)
            {
                if (archive.Entries.Count > SignatureArchive.MaxEntryCount)
                {
                    LogAlways($"SignatureArchiveHasTooManyEntries: the signature archive has {archive.Entries.Count} entries; the limit is {SignatureArchive.MaxEntryCount}.");
                    return ReturnCode.SignatureArchiveHasTooManyEntries;
                }

                // ---- manifest present / readable ----------------------------
                var manifestEntry = archive.Entries
                    .FirstOrDefault(e => ManifestCodec.Normalize(e.FullName) == SignatureArchive.ManifestEntryName);
                if (manifestEntry is null)
                {
                    LogAlways($"SignatureManifestIsMissing: the signature archive has no {SignatureArchive.ManifestEntryName} entry.");
                    return ReturnCode.SignatureManifestIsMissing;
                }

                byte[] manifestBytes;
                try
                {
                    manifestBytes = ZipSupport.ReadEntry(manifestEntry);
                }
                catch (Exception ex) when (ex is InvalidDataException or IOException)
                {
                    LogAlways($"SignatureManifestIsUnreadable: {ex.Message}");
                    return ReturnCode.SignatureManifestIsUnreadable;
                }
                if (manifestBytes.Length == 0)
                {
                    LogAlways("SignatureManifestIsUnreadable: the signature manifest is empty.");
                    return ReturnCode.SignatureManifestIsUnreadable;
                }
                Log($"Manifest file size:  {manifestBytes.Length}");
                Log($"Manifest file hash:  {Convert.ToHexString(SHA256.HashData(manifestBytes))}");

                // ---- signature present / readable ---------------------------
                var signatureEntry = archive.Entries
                    .FirstOrDefault(e => ManifestCodec.Normalize(e.FullName) == SignatureArchive.SignatureEntryName);
                if (signatureEntry is null)
                {
                    LogAlways($"SignatureIsMissing: the signature archive has no {SignatureArchive.SignatureEntryName} entry.");
                    return ReturnCode.SignatureIsMissing;
                }

                byte[] signatureBytes;
                try
                {
                    signatureBytes = ZipSupport.ReadEntry(signatureEntry);
                }
                catch (Exception ex) when (ex is InvalidDataException or IOException)
                {
                    LogAlways($"SignatureIsUnreadable: {ex.Message}");
                    return ReturnCode.SignatureIsUnreadable;
                }
                if (signatureBytes.Length == 0)
                {
                    LogAlways("SignatureIsUnreadable: the signature is empty.");
                    return ReturnCode.SignatureIsUnreadable;
                }
                Log($"Signature file size:  {signatureBytes.Length}");
                Log($"Signature file hash:  {Convert.ToHexString(SHA256.HashData(signatureBytes))}");

                // ---- manifest content --------------------------------------
                // The manifest is parsed before the signature is checked (which is
                // also the order Microsoft's implementation uses), but nothing from
                // it is trusted until VerifySignature succeeds below.
                var manifest = ManifestCodec.TryRead(manifestBytes);
                if (manifest?.Package is null || manifest.Entries is null)
                {
                    LogAlways("SignatureManifestIsInvalid: the signature manifest is not a valid manifest document.");
                    return ReturnCode.SignatureManifestIsInvalid;
                }

                // ---- signature and certificate chain ------------------------
                var signatureResult = VerifySignature(manifestBytes, signatureBytes);
                if (signatureResult != ReturnCode.Success) return signatureResult;

                // ---- entry integrity ---------------------------------------
                var entryResult = VerifyEntries(package, manifest);
                if (entryResult != ReturnCode.Success) return entryResult;

                // ---- package integrity -------------------------------------
                var packageResult = VerifyPackage(packagePath, packageSize, manifest);
                if (packageResult != ReturnCode.Success) return packageResult;
            }
        }

        LogSuccess("Success");
        return ReturnCode.Success;
    }

    // -------------------------------------------------------------------------
    // Signature / trust
    // -------------------------------------------------------------------------

    private ReturnCode VerifySignature(byte[] manifestBytes, byte[] signatureBytes)
    {
        SignedData signedData;
        try
        {
            signedData = SignedData.Parse(signatureBytes, manifestBytes);
        }
        catch (Exception ex) when (ex is System.Formats.Asn1.AsnContentException or CryptographicException)
        {
            LogAlways($"SignatureIsInvalid: the signature is not a readable CMS SignedData structure ({ex.Message}).");
            if (_options.Verbose) LogAlways(ex.ToString());
            return ReturnCode.SignatureIsInvalid;
        }

        if (signedData.SignerInfos.Count != 1)
        {
            LogAlways($"SignatureIsInvalid: the signature has {signedData.SignerInfos.Count} signers; exactly one is expected.");
            return ReturnCode.SignatureIsInvalid;
        }

        var signer = signedData.SignerInfos[0];
        Log($"The signature carries {signedData.Certificates.Count} certificate(s).");
        foreach (var certificate in signedData.Certificates)
            Log($"  certificate: {certificate.Subject} (serial {certificate.SerialNumber})");
        if (signer.Certificate is null)
        {
            LogAlways("SignatureIsInvalid: the signing certificate is not present in the signature.");
            return ReturnCode.SignatureIsInvalid;
        }

        Log("Primary signature type:  publisher signature");
        Log($"Publisher primary signature:  {signer.Certificate.Subject}");

        var check = CmsSignature.Verify(signedData, signer, manifestBytes, detached: true, out var detail);
        if (check != SignatureCheckResult.Ok)
        {
            LogAlways($"SignatureIsInvalid: {detail}.");
            return ReturnCode.SignatureIsInvalid;
        }
        Log("SignatureIntegrityPolicy for primary signature succeeded.");

        // The certificate chain is evaluated at the signing time when the signature
        // carries a valid RFC 3161 timestamp, otherwise at the current time.
        var timestamp = Timestamp.Read(signer);
        DateTimeOffset effectiveTime = DateTimeOffset.UtcNow;
        if (timestamp is not null)
        {
            if (timestamp.Check != TimestampCheck.Ok)
            {
                LogAlways($"SignatureIsInvalid: the timestamp token is invalid ({timestamp.Detail}).");
                return ReturnCode.SignatureIsInvalid;
            }
            effectiveTime = timestamp.GenTime!.Value;
            Log($"Timestamp signature status:  valid at {effectiveTime:u}");
        }
        else
        {
            Log("Timestamp signature status:  the signature is not timestamped.");
        }

        // ---- publisher signature trust ------------------------------------
        var publisherChain = _chains.Build(signer.Certificate, ToStore(signedData.Certificates, signer.Certificate), effectiveTime);
        if (!publisherChain.Trusted)
        {
            LogAlways($"SignatureTrustPolicy failed for publisher signature: {publisherChain.Detail}");
            return publisherChain.Failure == TrustFailure.Revoked ? ReturnCode.CertificateRevoked : ReturnCode.Untrusted;
        }
        Log("SignatureTrustPolicy for publisher signature succeeded.");

        var revocation = CheckRevocation(publisherChain, timestamp?.Certificates);
        if (revocation is not null) return revocation.Value;

        // VsMarketplacePolicy: the primary signature must be a Visual Studio
        // Marketplace publisher signature, or carry a Visual Studio Marketplace
        // repository countersignature over the primary signature.
        bool isMarketplace = IsMarketplacePublisher(signer.Certificate);
        if (!isMarketplace || _options.RequireRepositoryCountersignature)
            isMarketplace = IsMarketplaceSigned(signedData, signer);
        if (!_options.RequireVisualStudioMarketplaceSignature || !isMarketplace)
        {
            if (_options.RequireVisualStudioMarketplaceSignature)
            {
                LogAlways("TrustedSignerPolicy failed.  The signature lacks a Visual Studio Marketplace repository primary signature or countersignature.");
                return ReturnCode.Untrusted;
            }
            Log("TrustedSignerPolicy skipped (Visual Studio Marketplace signature not required).");
            return ReturnCode.Success;
        }

        if (!HasEku(signer.Certificate, CodeSigningEku))
        {
            LogAlways($"SignatureIsNotValid: the signing certificate has no code signing EKU ({CodeSigningEku}).");
            return ReturnCode.SignatureIsNotValid;
        }

        if (HasEku(signer.Certificate, LifetimeSigningEku))
        {
            LogAlways($"SignatureIsNotValid: the signing certificate has the lifetime signing EKU ({LifetimeSigningEku}).");
            return ReturnCode.SignatureIsNotValid;
        }

        if (timestamp is not null)
        {
            var timestampChain = _chains.Build(
                timestamp.SignerCertificate!,
                ToStore(timestamp.Certificates, timestamp.SignerCertificate!),
                timestamp.GenTime!.Value);
            if (!timestampChain.Trusted)
            {
                LogAlways($"SignatureTrustPolicy failed for timestamp signature: {timestampChain.Detail}");
                return timestampChain.Failure == TrustFailure.Revoked ? ReturnCode.CertificateRevoked : ReturnCode.Untrusted;
            }
            var timestampRevocation = CheckRevocation(timestampChain, null);
            if (timestampRevocation is not null) return timestampRevocation.Value;
        }

        if (_options.RequirePublisherCertificateTimeValid &&
            (signer.Certificate.NotBefore.ToUniversalTime() > DateTime.UtcNow ||
             signer.Certificate.NotAfter.ToUniversalTime() < DateTime.UtcNow))
        {
            LogAlways("SignatureValidityPolicy failed.  The publisher primary signature's certificate is outside of its validity period.");
            return ReturnCode.SignatureIsNotValid;
        }

        Log("VsMarketplacePolicy for primary signature succeeded.");
        return ReturnCode.Success;
    }

    private static X509Certificate2Collection ToStore(IEnumerable<X509Certificate2> certificates, X509Certificate2 exclude)
    {
        var store = new X509Certificate2Collection();
        foreach (var certificate in certificates)
        {
            if (!certificate.RawData.AsSpan().SequenceEqual(exclude.RawData)) store.Add(certificate);
        }
        return store;
    }

    private static bool HasEku(X509Certificate2 certificate, string oid)
    {
        foreach (var extension in certificate.Extensions)
        {
            if (extension is X509EnhancedKeyUsageExtension eku)
            {
                foreach (var usage in eku.EnhancedKeyUsages)
                    if (usage.Value == oid) return true;
            }
        }
        return false;
    }

    private static bool IsMarketplacePublisher(X509Certificate2 certificate) =>
        certificate.Subject.Contains(MarketplaceOrganizationalUnit, StringComparison.OrdinalIgnoreCase) ||
        certificate.Subject.Contains(MarketplacePpeOrganizationalUnit, StringComparison.OrdinalIgnoreCase);

    /// <summary>
    /// Resolves another certificate carried by the same signature (for example the
    /// repository certificate of a countersignature) by subject key identifier or
    /// issuer/serial.
    /// </summary>
    private static X509Certificate2? ResolveCertificate(SignedData signedData, SignerInfo signer)
    {
        foreach (var certificate in signedData.Certificates)
        {
            if (signer.SubjectKeyIdentifier is not null)
            {
                var extension = certificate.Extensions["2.5.29.14"];
                if (extension is null) continue;
                byte[] inner;
                try
                {
                    inner = new System.Formats.Asn1.AsnReader(extension.RawData, System.Formats.Asn1.AsnEncodingRules.BER).ReadOctetString();
                }
                catch (System.Formats.Asn1.AsnContentException)
                {
                    inner = extension.RawData;
                }
                if (inner.AsSpan().SequenceEqual(signer.SubjectKeyIdentifier)) return certificate;
            }
            else if (signer.IssuerDer.Length != 0 &&
                     certificate.IssuerName.RawData.AsSpan().SequenceEqual(signer.IssuerDer) &&
                     certificate.SerialNumberBytes.Span.SequenceEqual(SignedData.SerialOf(signer)))
            {
                return certificate;
            }
        }
        return null;
    }

    /// <summary>
    /// Accepts either a Visual Studio Marketplace publisher signature or a
    /// Visual Studio Marketplace repository countersignature over the primary
    /// signature.
    /// </summary>
    private bool IsMarketplaceSigned(SignedData signedData, SignerInfo signer)
    {
        var countersignature = signer.FindUnsignedAttribute(Timestamp.OidCounterSignature);
        if (countersignature is null)
        {
            Log("LibraryBuildPolicy: the signature has no repository countersignature.");
            return false;
        }

        foreach (var value in countersignature.Values)
        {
            SignerInfo inner;
            try
            {
                inner = SignedData.ParseSignerInfo(value);
            }
            catch (Exception ex) when (ex is System.Formats.Asn1.AsnContentException or CryptographicException)
            {
                continue;
            }

            var certificate = ResolveCertificate(signedData, inner);
            inner.Certificate = certificate;
            if (certificate is null)
            {
                Log("LibraryBuildPolicy: the countersignature certificate is not carried by the signature.");
                continue;
            }
            if (!IsMarketplacePublisher(certificate))
            {
                Log($"LibraryBuildPolicy: the countersignature certificate ({certificate.Subject}) is not a Visual Studio Marketplace certificate.");
                continue;
            }

            var result = CmsSignature.Verify(signedData, inner, signer.Signature, detached: false, out var detail);
            if (result != SignatureCheckResult.Ok)
            {
                Log($"LibraryBuildPolicy: the repository countersignature is invalid ({detail}).");
                continue;
            }

            // The countersignature's own timestamp (if any) fixes the effective time.
            var countersignatureTimestamp = Timestamp.Read(inner);
            var effectiveTime = countersignatureTimestamp?.Check == TimestampCheck.Ok
                ? countersignatureTimestamp.GenTime!.Value
                : DateTimeOffset.UtcNow;
            var chain = _chains.Build(certificate, ToStore(signedData.Certificates, certificate), effectiveTime);
            if (!chain.Trusted)
            {
                Log($"LibraryBuildPolicy: the repository countersignature certificate is not trusted ({chain.Detail}).");
                continue;
            }

            Log("LibraryBuildPolicy accepted the Visual Studio Marketplace repository countersignature.");
            return true;
        }
        return false;
    }

    // -------------------------------------------------------------------------
    // Revocation
    // -------------------------------------------------------------------------

    private ReturnCode? CheckRevocation(ChainResult chain, List<X509Certificate2>? extraCertificates)
    {
        if (!_options.RequireRevocationStatus && !_options.RequireRevocationCheck)
        {
            Log("Revocation checking is disabled (matches Microsoft's default: --requirerevocationstatus is not implied).");
            return null;
        }

        if (chain.BuiltChain is null) return null;
        var revocation = RevocationChecker.Evaluate(chain.BuiltChain, extraCertificates, _options.Diagnostics, _options.Verbose);
        if (revocation is null) return null;
        LogAlways($"CertificateRevoked: {revocation}");
        return ReturnCode.CertificateRevoked;
    }

    // -------------------------------------------------------------------------
    // Entries
    // -------------------------------------------------------------------------

    private ReturnCode VerifyEntries(ZipArchive package, SignatureManifest manifest)
    {
        var entries = new Dictionary<string, ZipArchiveEntry>(StringComparer.Ordinal);
        foreach (var entry in package.Entries)
        {
            var name = ManifestCodec.Normalize(entry.FullName);
            entries.TryAdd(name, entry);
        }

        var referenced = new HashSet<string>(StringComparer.Ordinal);
        foreach (var (key, expected) in manifest.Entries!)
        {
            var name = ManifestCodec.DecodeEntryName(key);
            if (name is null)
            {
                LogAlways($"SignatureManifestIsInvalid: the entry key '{key}' is not valid base64.");
                return ReturnCode.SignatureManifestIsInvalid;
            }
            referenced.Add(name);

            if (!entries.TryGetValue(name, out var entry))
            {
                LogAlways($"EntryIsMissing: '{name}' is in the signature manifest but not in the package.");
                return ReturnCode.EntryIsMissing;
            }

            if (expected.Digests is null ||
                !expected.Digests.TryGetValue(ManifestCodec.Sha256DigestName, out var expectedDigestBase64))
            {
                LogAlways($"SignatureManifestIsInvalid: the entry '{name}' has no sha256 digest.");
                return ReturnCode.SignatureManifestIsInvalid;
            }

            byte[] expectedDigest;
            try
            {
                expectedDigest = Convert.FromBase64String(expectedDigestBase64);
            }
            catch (FormatException)
            {
                LogAlways($"SignatureManifestIsInvalid: the entry '{name}' has an invalid base64 sha256 digest.");
                return ReturnCode.SignatureManifestIsInvalid;
            }

            if (entry.Length != expected.Size)
            {
                LogAlways($"EntryIsTampered: '{name}' has size {entry.Length} in the package but {expected.Size} in the signature manifest.");
                return ReturnCode.EntryIsTampered;
            }

            byte[] actualDigest;
            try
            {
                using var stream = entry.Open();
                actualDigest = SHA256.HashData(stream);
            }
            catch (Exception ex) when (ex is InvalidDataException or IOException)
            {
                LogAlways($"EntryIsTampered: '{name}' could not be read ({ex.Message}).");
                return ReturnCode.EntryIsTampered;
            }

            if (!CryptographicOperations.FixedTimeEquals(actualDigest, expectedDigest))
            {
                LogAlways($"EntryIsTampered: '{name}' does not match the sha256 digest in the signature manifest.");
                return ReturnCode.EntryIsTampered;
            }

            Log($"ManifestEntriesIntegrityPolicy: entry '{name}' verified.");
        }
        Log("ManifestEntriesIntegrityPolicy succeeded.");

        if (_options.RequireNoExtraneousEntries)
        {
            foreach (var name in entries.Keys)
            {
                if (!referenced.Contains(name))
                {
                    LogAlways($"SignatureIntegrityCheckFailed: the package contains '{name}', which the signature manifest does not list.");
                    return ReturnCode.SignatureIntegrityCheckFailed;
                }
            }
            Log("The package contains no extraneous entries.");
        }
        else
        {
            // Microsoft's verifier tolerates unreferenced entries: the package level
            // digest is what covers the rest of the archive.
            Log("Extra entries in the package are not rejected (RequireNoExtraneousEntries is false).");
        }

        return ReturnCode.Success;
    }

    private ReturnCode VerifyPackage(string packagePath, long packageSize, SignatureManifest manifest)
    {
        var expected = manifest.Package!;
        if (expected.Size != packageSize)
        {
            LogAlways($"ManifestPackageIntegrityPolicy failed:  unexpected package size (expected {expected.Size}, actual {packageSize}).");
            return ReturnCode.PackageIntegrityCheckFailed;
        }

        if (expected.Digests is null ||
            !expected.Digests.TryGetValue(ManifestCodec.Sha256DigestName, out var expectedBase64))
        {
            LogAlways("SignatureManifestIsInvalid: the package entry has no sha256 digest.");
            return ReturnCode.SignatureManifestIsInvalid;
        }

        byte[] expectedDigest;
        try
        {
            expectedDigest = Convert.FromBase64String(expectedBase64);
        }
        catch (FormatException)
        {
            LogAlways("SignatureManifestIsInvalid: the package entry has an invalid base64 sha256 digest.");
            return ReturnCode.SignatureManifestIsInvalid;
        }

        byte[] actualDigest;
        try
        {
            actualDigest = ZipSupport.HashFile(packagePath);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            LogAlways($"PackageIsUnreadable: {ex.Message}");
            return ReturnCode.PackageIsUnreadable;
        }
        Log($"Package file hash:  {Convert.ToHexString(actualDigest)}");

        if (!CryptographicOperations.FixedTimeEquals(actualDigest, expectedDigest))
        {
            LogAlways("ManifestPackageIntegrityPolicy failed:  integrity check failed.");
            return ReturnCode.PackageIntegrityCheckFailed;
        }

        Log("ManifestPackageIntegrityPolicy succeeded.");
        return ReturnCode.Success;
    }

    // -------------------------------------------------------------------------
    // Manifest generation / archive creation
    // -------------------------------------------------------------------------

    public ReturnCode GenerateManifest(string packagePath, string outputPath, bool overwrite)
    {
        if (!File.Exists(packagePath))
        {
            LogAlways($"PackageIsUnreadable: the package file '{packagePath}' does not exist.");
            return ReturnCode.PackageIsUnreadable;
        }

        if (File.Exists(outputPath) && !overwrite)
        {
            LogAlways($"FileAlreadyExists: '{outputPath}' already exists.");
            return ReturnCode.FileAlreadyExists;
        }

        long packageSize;
        try
        {
            packageSize = ZipSupport.FileLength(packagePath);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            LogAlways($"PackageIsUnreadable: {ex.Message}");
            return ReturnCode.PackageIsUnreadable;
        }

        ZipArchive package;
        try
        {
            package = ZipSupport.Open(packagePath, packageSize);
        }
        catch (ZipFormatException ex)
        {
            LogAlways($"PackageIsInvalidZip: {ex.Message}");
            return ReturnCode.PackageIsInvalidZip;
        }

        byte[] packageDigest;
        var manifest = new SignatureManifest
        {
            Package = new ManifestPackage
            {
                Size = packageSize,
                Digests = new Dictionary<string, string>(),
            },
            Entries = new Dictionary<string, ManifestEntry>(StringComparer.Ordinal),
        };

        try
        {
            using (package)
            {
                foreach (var entry in package.Entries)
                {
                    var name = ManifestCodec.Normalize(entry.FullName);
                    if (manifest.Entries.ContainsKey(Convert.ToBase64String(Encoding.UTF8.GetBytes(name))))
                        continue;
                    using var stream = entry.Open();
                    var digest = SHA256.HashData(stream);
                    manifest.Entries[Convert.ToBase64String(Encoding.UTF8.GetBytes(name))] = new ManifestEntry
                    {
                        Size = entry.Length,
                        Digests = new Dictionary<string, string> { [ManifestCodec.Sha256DigestName] = Convert.ToBase64String(digest) },
                    };
                    Log($"Manifest entry: '{name}' ({entry.Length} bytes)");
                }
            }
            packageDigest = ZipSupport.HashFile(packagePath);
        }
        catch (Exception ex) when (ex is InvalidDataException or IOException)
        {
            LogAlways($"PackageIsUnreadable: {ex.Message}");
            return ReturnCode.PackageIsUnreadable;
        }

        manifest.Package.Digests[ManifestCodec.Sha256DigestName] = Convert.ToBase64String(packageDigest);

        try
        {
            var bytes = ManifestCodec.Write(manifest);
            File.WriteAllBytes(outputPath, bytes);
            Log($"Manifest file size:  {bytes.Length}");
            Log($"Manifest file hash:  {Convert.ToHexString(SHA256.HashData(bytes))}");
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            LogAlways($"UnknownError: the manifest could not be written ({ex.Message}).");
            return ReturnCode.UnknownError;
        }

        LogSuccess("Success");
        return ReturnCode.Success;
    }

    public ReturnCode CreateSignatureArchive(string manifestPath, string signaturePath, string outputPath, bool overwrite)
    {
        if (!File.Exists(manifestPath))
        {
            LogAlways($"SignatureManifestIsUnreadable: '{manifestPath}' does not exist.");
            return ReturnCode.SignatureManifestIsUnreadable;
        }

        if (!File.Exists(signaturePath))
        {
            LogAlways($"SignatureIsUnreadable: '{signaturePath}' does not exist.");
            return ReturnCode.SignatureIsUnreadable;
        }

        if (File.Exists(outputPath) && !overwrite)
        {
            LogAlways($"FileAlreadyExists: '{outputPath}' already exists.");
            return ReturnCode.FileAlreadyExists;
        }

        if (Path.GetFullPath(manifestPath) == Path.GetFullPath(signaturePath))
        {
            LogAlways("SignatureArchiveHasSameSignatureFile: the manifest and signature paths are the same file.");
            return ReturnCode.SignatureArchiveHasSameSignatureFile;
        }

        try
        {
            using var output = new FileStream(outputPath, FileMode.Create, FileAccess.Write);
            using var zip = new ZipArchive(output, ZipArchiveMode.Create);
            AddEntry(zip, SignatureArchive.ManifestEntryName, File.ReadAllBytes(manifestPath));
            AddEntry(zip, SignatureArchive.SignatureEntryName, File.ReadAllBytes(signaturePath));
            Log($"Signature archive file size:  {output.Length}");
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            LogAlways($"SignatureArchiveIsUnreadable: the signature archive could not be written ({ex.Message}).");
            return ReturnCode.SignatureArchiveIsUnreadable;
        }

        LogSuccess("Success");
        return ReturnCode.Success;
    }

    private static void AddEntry(ZipArchive archive, string name, byte[] content)
    {
        var entry = archive.CreateEntry(name, CompressionLevel.Optimal);
        entry.LastWriteTime = DateTimeOffset.UtcNow;
        using var stream = entry.Open();
        stream.Write(content);
    }
}
