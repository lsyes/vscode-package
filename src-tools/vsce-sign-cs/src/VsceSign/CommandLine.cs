using System.Security.Cryptography.X509Certificates;
using System.Text;
using VsceSign.Crypto;

namespace VsceSign;

internal sealed class CommandLineOptions
{
    public string? Command { get; set; }
    public bool Help { get; set; }
    public bool Verbose { get; set; }
    public string? Package { get; set; }
    public string? SignatureArchivePath { get; set; }
    public string? Manifest { get; set; }
    public string? Signature { get; set; }
    public string? Output { get; set; }
    public string? Certificate { get; set; }
    public string? HashAlgorithm { get; set; }
    public string? TimestampService { get; set; }
    public string? AdditionalPolicy { get; set; }
    public bool IsPpe { get; set; }
    public bool RequireMicrosoftPublisherSignature { get; set; }
    public bool RequireNoExtraneousEntries { get; set; }
    public bool RequireOnlyPublisherSignatureAndTimestamp { get; set; }
    public bool RequirePublisherCertificateTimeValid { get; set; }
    public bool RequireRevocationStatus { get; set; }
    public bool RequireVsmpRepositoryPrimarySignatureOrCountersignature { get; set; }
    public bool Overwrite { get; set; }
    public bool Error { get; set; }
    public string? ErrorMessage { get; set; }
    public string? MissingArgument { get; set; }
}

/// <summary>
/// The argument grammar observed in the Microsoft vsce-sign 2.0.8 binary:
/// <c>generatemanifest</c>, <c>verify</c> and <c>zip</c> with the option set
/// below. Options accept both <c>--name value</c> and <c>--name=value</c>, are
/// case-insensitive, and have the single-letter aliases found in the binary's
/// string table.
/// </summary>
internal static class CommandLine
{
    public const string ProductName = "vsce-sign";
    public const string Version = "2.0.8";

    private static readonly Dictionary<string, Action<CommandLineOptions, string?>> Options = new(StringComparer.OrdinalIgnoreCase)
    {
        ["-v"] = (o, _) => o.Verbose = true,
        ["--verbose"] = (o, _) => o.Verbose = true,
        ["-p"] = (o, v) => o.Package = v,
        ["--package"] = (o, v) => o.Package = v,
        ["-sa"] = (o, v) => o.SignatureArchivePath = v,
        ["--signaturearchive"] = (o, v) => o.SignatureArchivePath = v,
        ["-m"] = (o, v) => o.Manifest = v,
        ["--manifest"] = (o, v) => o.Manifest = v,
        ["-s"] = (o, v) => o.Signature = v,
        ["--signature"] = (o, v) => o.Signature = v,
        ["-o"] = (o, v) => o.Output = v,
        ["--output"] = (o, v) => o.Output = v,
        ["-c"] = (o, v) => o.Certificate = v,
        ["--certificate"] = (o, v) => o.Certificate = v,
        ["-ha"] = (o, v) => o.HashAlgorithm = v,
        ["--hashalgorithm"] = (o, v) => o.HashAlgorithm = v,
        ["-ts"] = (o, v) => o.TimestampService = v,
        ["--timestampservice"] = (o, v) => o.TimestampService = v,
        ["-ap"] = (o, v) => o.AdditionalPolicy = v,
        ["--additionalpolicy"] = (o, v) => o.AdditionalPolicy = v,
        ["-z"] = (o, _) => o.Overwrite = true,
        ["--zip"] = (o, _) => o.Overwrite = true,
        ["--isppe"] = (o, _) => o.IsPpe = true,
        ["--requiremicrosoftpublishersignature"] = (o, _) => o.RequireMicrosoftPublisherSignature = true,
        ["--requirenoextraneousentries"] = (o, _) => o.RequireNoExtraneousEntries = true,
        ["--requireonlypublishersignatureandtimestamp"] = (o, _) => o.RequireOnlyPublisherSignatureAndTimestamp = true,
        ["--requirepublishercertificatebetimevalid"] = (o, _) => o.RequirePublisherCertificateTimeValid = true,
        ["--requirerevocationstatus"] = (o, _) => o.RequireRevocationStatus = true,
        ["--requirevsmprepositoryprimarysignatureorcountersignature"] = (o, _) => o.RequireVsmpRepositoryPrimarySignatureOrCountersignature = true,
    };

    private static readonly HashSet<string> ValueOptions = new(StringComparer.OrdinalIgnoreCase)
    {
        "-p", "--package", "-sa", "--signaturearchive", "-m", "--manifest", "-s", "--signature",
        "-o", "--output", "-c", "--certificate", "-ha", "--hashalgorithm", "-ts", "--timestampservice",
        "-ap", "--additionalpolicy",
    };

    public static CommandLineOptions Parse(string[] args)
    {
        var options = new CommandLineOptions();
        for (int i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            if (string.IsNullOrEmpty(arg)) continue;

            if (arg is "-h" or "-?" or "/?" or "--help" || arg.Equals("-help", StringComparison.OrdinalIgnoreCase))
            {
                options.Help = true;
                continue;
            }

            if (!arg.StartsWith('-'))
            {
                if (options.Command is null)
                {
                    options.Command = arg.ToLowerInvariant();
                    continue;
                }
                options.Error = true;
                options.ErrorMessage = $"Unexpected argument '{arg}'.";
                return options;
            }

            string name = arg;
            string? inlineValue = null;
            int equals = arg.IndexOf('=');
            if (equals > 0)
            {
                name = arg[..equals];
                inlineValue = arg[(equals + 1)..];
            }

            if (!Options.TryGetValue(name, out var setter))
            {
                options.Error = true;
                options.ErrorMessage = $"Unknown option '{name}'.";
                return options;
            }

            if (ValueOptions.Contains(name))
            {
                string? value = inlineValue;
                if (value is null)
                {
                    if (i + 1 >= args.Length || args[i + 1].StartsWith('-') && args[i + 1].Length > 1 && !char.IsDigit(args[i + 1][1]))
                    {
                        options.Error = true;
                        options.ErrorMessage = $"The option '{name}' requires a value.";
                        return options;
                    }
                    value = args[++i];
                }
                setter(options, value);
            }
            else
            {
                setter(options, inlineValue);
            }
        }

        return options;
    }

    public static void WriteUsage(TextWriter writer)
    {
        writer.WriteLine($"{ProductName} {Version}");
        writer.WriteLine("Extension package signing and verification tool for Visual Studio Code.");
        writer.WriteLine();
        writer.WriteLine($"Usage: {ProductName} <command> [options]");
        writer.WriteLine();
        writer.WriteLine("Commands:");
        writer.WriteLine("  verify           Verify the signature of an extension package.");
        writer.WriteLine("  generatemanifest Generate the signature manifest for an extension package.");
        writer.WriteLine("  zip              Create the signature archive from a manifest and a signature.");
        writer.WriteLine();
        writer.WriteLine("Options:");
        writer.WriteLine("  -p, --package <path>            Path to the extension package (.vsix).");
        writer.WriteLine("  -sa, --signaturearchive <path>  Path to the signature archive (.signature.zip).");
        writer.WriteLine("  -m, --manifest <path>           Path to the signature manifest (.signature.manifest).");
        writer.WriteLine("  -s, --signature <path>          Path to the signature file (.signature.p7s).");
        writer.WriteLine("  -o, --output <path>             Path of the file to create.");
        writer.WriteLine("  -c, --certificate <path>        Path to a code signing certificate.");
        writer.WriteLine("  -ha, --hashalgorithm <name>     Hash algorithm (SHA256, SHA384, SHA512).");
        writer.WriteLine("  -ts, --timestampservice <url>   RFC 3161 timestamp service URL.");
        writer.WriteLine("  -ap, --additionalpolicy <name>  Additional policy to require.");
        writer.WriteLine("  -z, --zip                       Overwrite an existing output file.");
        writer.WriteLine("  -v, --verbose                   Write diagnostic output to stderr.");
        writer.WriteLine("      --isppe                     Require a Visual Studio Marketplace (PPE) signature.");
        writer.WriteLine("      --requiremicrosoftpublishersignature");
        writer.WriteLine("      --requirenoextraneousentries");
        writer.WriteLine("      --requireonlypublishersignatureandtimestamp");
        writer.WriteLine("      --requirepublishercertificatebetimevalid");
        writer.WriteLine("      --requirerevocationstatus");
        writer.WriteLine("      --requirevsmprepositoryprimarysignatureorcountersignature");
        writer.WriteLine("  -h, --help                      Show this help text.");
        writer.WriteLine();
        writer.WriteLine("Return codes:");
        foreach (ReturnCode code in Enum.GetValues<ReturnCode>())
            writer.WriteLine($"  {(int)code,3}  {code}");
    }
}
