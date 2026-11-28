namespace VsceSign;

/// <summary>
/// Return codes of Microsoft.VisualStudio.Extensions.Signing.ReturnCode, as
/// documented by the @vscode/vsce-sign JavaScript wrapper and confirmed by the
/// strings in the Microsoft vsce-sign 2.0.8 binary.
/// </summary>
internal enum ReturnCode
{
    Success = 0,
    RequiredArgumentMissing = 3,
    InvalidArgument = 4,
    PackageIsUnreadable = 5,
    UnhandledException = 6,
    SignatureManifestIsMissing = 7,
    SignatureManifestIsUnreadable = 8,
    SignatureIsMissing = 9,
    SignatureIsUnreadable = 10,
    CertificateIsUnreadable = 11,
    SignatureArchiveIsUnreadable = 12,
    FileAlreadyExists = 13,
    SignatureArchiveIsInvalidZip = 14,
    SignatureArchiveHasSameSignatureFile = 15,

    PackageIntegrityCheckFailed = 30,
    SignatureIsInvalid = 31,
    SignatureManifestIsInvalid = 32,
    SignatureIntegrityCheckFailed = 33,
    EntryIsMissing = 34,
    EntryIsTampered = 35,
    Untrusted = 36,
    CertificateRevoked = 37,
    SignatureIsNotValid = 38,
    UnknownError = 39,
    PackageIsInvalidZip = 40,
    SignatureArchiveHasTooManyEntries = 41,
}

/// <summary>Well-known names inside the signature archive.</summary>
internal static class SignatureArchive
{
    public const string ManifestEntryName = ".signature.manifest";
    public const string SignatureEntryName = ".signature.p7s";
    /// <summary>Microsoft's cap on the number of entries in a signature archive.</summary>
    public const int MaxEntryCount = 10;
    public const string DefaultFileName = ".signature.zip";
}
