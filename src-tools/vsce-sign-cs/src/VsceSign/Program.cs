using System.Text;
using VsceSign.Crypto;

namespace VsceSign;

internal static class Program
{
    private static int Main(string[] args)
    {
        try
        {
            return (int)Run(args);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"UnhandledException: {ex}");
            return (int)ReturnCode.UnhandledException;
        }
    }

    /// <summary>
    /// Diagnostic sink: lines go to stderr (as documented) and, while --verbose is
    /// in effect, are mirrored to stdout at the end of the run so that callers that
    /// capture stdout (the @vscode/vsce-sign JavaScript wrapper only reads stdout)
    /// still see them.
    /// </summary>
    internal sealed class DiagnosticWriter : TextWriter
    {
        private readonly StringBuilder _mirror = new();
        public bool Mirror { get; set; }

        public override Encoding Encoding => Console.Error.Encoding;

        public override void WriteLine(string? value)
        {
            Console.Error.WriteLine(value);
            if (Mirror) _mirror.AppendLine(value);
        }

        public override void Write(string? value) => WriteLine(value);

        public void FlushMirror()
        {
            if (!Mirror || _mirror.Length == 0) return;
            Console.Out.Write(_mirror.ToString());
            Console.Out.Flush();
            _mirror.Clear();
        }
    }

    private static ReturnCode Run(string[] args)
    {
        if (args.Length == 0)
        {
            CommandLine.WriteUsage(Console.Out);
            return ReturnCode.RequiredArgumentMissing;
        }

        var options = CommandLine.Parse(args);

        if (options.Help)
        {
            CommandLine.WriteUsage(Console.Out);
            return ReturnCode.Success;
        }

        if (options.Error)
        {
            Console.Error.WriteLine($"InvalidArgument: {options.ErrorMessage}");
            CommandLine.WriteUsage(Console.Error);
            return ReturnCode.InvalidArgument;
        }

        if (options.Command is null)
        {
            Console.Error.WriteLine("RequiredArgumentMissing: a command is required.");
            CommandLine.WriteUsage(Console.Error);
            return ReturnCode.RequiredArgumentMissing;
        }

        var diagnostics = new DiagnosticWriter { Mirror = options.Verbose };
        var (anchors, bundleCount, pinCount) = TrustStore.Load();
        if (options.Verbose)
        {
            diagnostics.WriteLine(
                $"Trust store: {anchors.Count} anchors ({bundleCount} from the embedded code signing fallback bundle, {pinCount} publisher roots).");
        }

        var chains = new ChainValidator(anchors);
        var verifier = new Verifier(
            new VerifierOptions
            {
                Verbose = options.Verbose,
                Diagnostics = diagnostics,
                // Microsoft's verifier always requires a Visual Studio Marketplace
                // publisher signature or repository countersignature.
                RequireVisualStudioMarketplaceSignature = true,
                RequireNoExtraneousEntries = options.RequireNoExtraneousEntries,
                RequirePublisherCertificateTimeValid = options.RequirePublisherCertificateTimeValid,
                RequireRevocationStatus = options.RequireRevocationStatus,
                RequireRepositoryCountersignature = options.RequireVsmpRepositoryPrimarySignatureOrCountersignature,
            },
            chains);

        var result = Dispatch(verifier, options);
        diagnostics.FlushMirror();
        return result;
    }

    private static ReturnCode Dispatch(Verifier verifier, CommandLineOptions options)
    {
        switch (options.Command)
        {
            case "verify":
                return RunVerify(verifier, options);

            case "generatemanifest":
                return RunGenerateManifest(verifier, options);

            case "zip":
                return RunZip(verifier, options);

            case "help":
                CommandLine.WriteUsage(Console.Out);
                return ReturnCode.Success;

            default:
                Console.Error.WriteLine($"InvalidArgument: unknown command '{options.Command}'.");
                CommandLine.WriteUsage(Console.Error);
                return ReturnCode.InvalidArgument;
        }
    }

    private static ReturnCode RunVerify(Verifier verifier, CommandLineOptions options)
    {
        if (string.IsNullOrEmpty(options.Package))
        {
            Console.Error.WriteLine("RequiredArgumentMissing: --package is required.");
            return ReturnCode.RequiredArgumentMissing;
        }

        if (string.IsNullOrEmpty(options.SignatureArchivePath))
        {
            Console.Error.WriteLine("RequiredArgumentMissing: --signaturearchive is required.");
            return ReturnCode.RequiredArgumentMissing;
        }

        var package = Path.GetFullPath(options.Package);
        var archive = Path.GetFullPath(options.SignatureArchivePath);

        var result = verifier.Verify(package, archive);
        if (options.Verbose) Console.Error.WriteLine($"Exit code:  {(int)result}");
        return result;
    }

    private static ReturnCode RunGenerateManifest(Verifier verifier, CommandLineOptions options)
    {
        if (string.IsNullOrEmpty(options.Package))
        {
            Console.Error.WriteLine("RequiredArgumentMissing: --package is required.");
            return ReturnCode.RequiredArgumentMissing;
        }

        var package = Path.GetFullPath(options.Package);
        var output = string.IsNullOrEmpty(options.Output)
            ? Path.Combine(Path.GetDirectoryName(package) ?? ".", SignatureArchive.ManifestEntryName)
            : Path.GetFullPath(options.Output);

        var result = verifier.GenerateManifest(package, output, options.Overwrite);
        if (result == ReturnCode.Success) Console.Out.WriteLine(output);
        if (options.Verbose) Console.Error.WriteLine($"Exit code:  {(int)result}");
        return result;
    }

    private static ReturnCode RunZip(Verifier verifier, CommandLineOptions options)
    {
        if (string.IsNullOrEmpty(options.Manifest))
        {
            Console.Error.WriteLine("RequiredArgumentMissing: --manifest is required.");
            return ReturnCode.RequiredArgumentMissing;
        }

        if (string.IsNullOrEmpty(options.Signature))
        {
            Console.Error.WriteLine("RequiredArgumentMissing: --signature is required.");
            return ReturnCode.RequiredArgumentMissing;
        }

        var manifest = Path.GetFullPath(options.Manifest);
        var signature = Path.GetFullPath(options.Signature);
        var output = string.IsNullOrEmpty(options.Output)
            ? Path.Combine(Path.GetDirectoryName(manifest) ?? ".", SignatureArchive.DefaultFileName)
            : Path.GetFullPath(options.Output);

        var result = verifier.CreateSignatureArchive(manifest, signature, output, options.Overwrite);
        if (result == ReturnCode.Success) Console.Out.WriteLine(output);
        if (options.Verbose) Console.Error.WriteLine($"Exit code:  {(int)result}");
        return result;
    }
}
