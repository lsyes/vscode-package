using System.IO.Compression;
using System.Security.Cryptography;

namespace VsceSign;

/// <summary>Raised when a ZIP archive cannot be read as a package or signature archive.</summary>
internal sealed class ZipFormatException : Exception
{
    public ZipFormatException(string message, Exception? inner = null) : base(message, inner) { }
}

internal static class ZipSupport
{
    /// <summary>
    /// Hashes the complete package file (the manifest's package digest covers the
    /// file bytes, including the trailing end-of-central-directory record).
    /// </summary>
    public static byte[] HashFile(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 16);
        return SHA256.HashData(stream);
    }

    public static long FileLength(string path) => new FileInfo(path).Length;

    /// <summary>
    /// Opens a ZIP archive for reading, mapping any structural problem to
    /// <see cref="ZipFormatException"/> so the caller can translate it into the
    /// documented return code.
    /// </summary>
    public static ZipArchive Open(string path, long expectedSize)
    {
        if (!File.Exists(path)) throw new FileNotFoundException(path);
        if (new FileInfo(path).Length == 0) throw new ZipFormatException("the archive is empty");
        if (expectedSize >= 0 && new FileInfo(path).Length != expectedSize)
            throw new ZipFormatException("the archive size is invalid");
        try
        {
            return ZipFile.OpenRead(path);
        }
        catch (InvalidDataException ex)
        {
            throw new ZipFormatException(ex.Message, ex);
        }
        catch (IOException ex)
        {
            throw new ZipFormatException(ex.Message, ex);
        }
    }

    public static byte[] ReadEntry(ZipArchiveEntry entry)
    {
        using var stream = entry.Open();
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        return buffer.ToArray();
    }
}
