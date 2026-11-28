using System.Text.Json;
using System.Text.Json.Serialization;

namespace VsceSign;

/// <summary>
/// The .signature.manifest document:
/// <code>
/// {"package":{"size":N,"digests":{"sha256":"&lt;base64&gt;"}},
///  "entries":{"&lt;base64 of UTF-8 path&gt;":{"size":N,"digests":{"sha256":"&lt;base64&gt;"}}}}
/// </code>
/// Property names are camel-cased, digests are raw (not hex) base64, and entry
/// keys are base64 of the UTF-8 entry path. Written without any whitespace,
/// which is what Microsoft's implementation produces.
/// </summary>
internal sealed class SignatureManifest
{
    public ManifestPackage? Package { get; set; }
    public Dictionary<string, ManifestEntry>? Entries { get; set; }
}

internal sealed class ManifestPackage
{
    public long Size { get; set; }
    public Dictionary<string, string>? Digests { get; set; }
}

internal sealed class ManifestEntry
{
    public long Size { get; set; }
    public Dictionary<string, string>? Digests { get; set; }
}

[JsonSourceGenerationOptions(
    PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase,
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    WriteIndented = false)]
[JsonSerializable(typeof(SignatureManifest))]
internal sealed partial class ManifestJsonContext : JsonSerializerContext
{
}

internal static class ManifestCodec
{
    public const string Sha256DigestName = "sha256";

    public static SignatureManifest? TryRead(byte[] bytes)
    {
        try
        {
            return JsonSerializer.Deserialize(bytes, ManifestJsonContext.Default.SignatureManifest);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    /// <summary>
    /// Serialises the manifest exactly the way Microsoft's implementation does:
    /// no whitespace, and without escaping '+', '/' or any other character inside
    /// the base64 digests and entry keys (the default System.Text.Json encoder
    /// escapes '+' as \u002B, which changes the bytes and therefore the signature).
    /// </summary>
    public static byte[] Write(SignatureManifest manifest) =>
        JsonSerializer.SerializeToUtf8Bytes(manifest, WritingContext.SignatureManifest);

    /// <summary>A source-generated context configured with the writer options above.</summary>
    private static readonly ManifestJsonContext WritingContext = new(new JsonSerializerOptions
    {
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = false,
    });

    public static string Base64Digest(byte[] hash) => Convert.ToBase64String(hash);

    /// <summary>Decodes a base64 manifest entry key back to the entry path.</summary>
    public static string? DecodeEntryName(string key)
    {
        try
        {
            var raw = Convert.FromBase64String(key);
            return System.Text.Encoding.UTF8.GetString(raw);
        }
        catch (FormatException)
        {
            return null;
        }
    }

    /// <summary>
    /// ZIP entry names are normalised to forward slashes; the manifest records the
    /// name exactly as it appears in the archive.
    /// </summary>
    public static string Normalize(string name) => name.Replace('\\', '/');
}
