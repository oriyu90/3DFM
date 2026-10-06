import Foundation

// MARK: - DTOs (single truth: the local server)

struct Job: Decodable, Identifiable, Hashable {
    var id: String
    var name: String
    var mode: String
    var state: String
    var stage: String
    var progress: Double
    var eta_s: Double?
    var error: String
    var seed: Int?
    var created_at: Double?
    var started_at: Double?
    var finished_at: Double?
}

struct Health: Decodable {
    var status: String
    var version: String?
    var gpu: String?
    var mem_total_gb: Double?
    var mem_free_gb: Double?
}

struct ModelInfo: Decodable {
    var id: String
    var present: Bool
    var size_gb: Double
    var expected_gb: Double
}

struct ModelsStatus: Decodable {
    var models: [ModelInfo]
    var runtimes: [String: RuntimeInfo]?
}

struct RuntimeInfo: Decodable {
    var present: Bool?
}

struct Artifact: Decodable {
    var path: String
    var size: Int
}

struct StorageInfo: Decodable {
    var data_dir: String
    var models_dir: String
    var output_dir: String?
    var models_inside_data: Bool?
    var data_size_gb: Double?
    var models_size_gb: Double?
    var free_gb: Double?
    var data_free_gb: Double?
    var default_data_dir: String?
    var default_models_dir: String?
    var configured_models_dir: String?
    var is_default_data: Bool?
    var is_default_models: Bool?
}

struct StorageMoveResult: Decodable {
    var ok: Bool?
    var models_dir: String?
    var data_dir: String?
    var path: String?
    var mode: String?
    var data_size_bytes: Int?
    var free_bytes: Int?
    var note: String?
}

// MARK: - Paths

enum Paths {
    /// Custom data dir persisted by Settings > Storage (UserDefaults).
    /// Empty = default. Mirrored to a plain pointer file so the CLI
    /// (Python) resolves the same location without the GUI running.
    static var customDataDirRaw: String {
        UserDefaults.standard.string(forKey: "customDataDir") ?? ""
    }

    static var dataDir: URL {
        if let o = ProcessInfo.processInfo.environment["FM3D_DATA_DIR"],
           !o.isEmpty {
            return URL(fileURLWithPath: (o as NSString).expandingTildeInPath)
        }
        let custom = customDataDirRaw.trimmingCharacters(in: .whitespacesAndNewlines)
        if !custom.isEmpty {
            return URL(fileURLWithPath: (custom as NSString).expandingTildeInPath)
        }
        // Fallback: pointer file written by CLI `3dfm storage set-data-dir`.
        if let p = Self.pointerFileDataDir(), !p.isEmpty {
            return URL(fileURLWithPath: (p as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/3DFM")
    }

    static var defaultDataDir: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/3DFM")
    }

    /// Plain pointer file shared with the Python CLI/server.
    static var pointerFile: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/3DFM.location")
    }

    private static func pointerFileDataDir() -> String? {
        let url = pointerFile
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        guard let s = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        let first = s.split(separator: "\n").first.map(String.init)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return first.isEmpty ? nil : first
    }

    /// Persist a custom data dir (UserDefaults + pointer file, atomically).
    /// Pass "" to revert to default. Throws on failure (caller reports).
    static func setCustomDataDir(_ path: String) throws {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            UserDefaults.standard.removeObject(forKey: "customDataDir")
            try? FileManager.default.removeItem(at: pointerFile)
            return
        }
        UserDefaults.standard.set(trimmed, forKey: "customDataDir")
        // Atomic pointer write (tmp + rename) so a crash never leaves
        // a half-written location.
        let tmp = pointerFile.deletingLastPathComponent()
            .appendingPathComponent(".3DFM-location-\(ProcessInfo.processInfo.processIdentifier)")
        try (trimmed + "\n").write(to: tmp, atomically: true, encoding: .utf8)
        if FileManager.default.fileExists(atPath: pointerFile.path) {
            _ = try? FileManager.default.replaceItemAt(pointerFile, withItemAt: tmp)
            // replaceItemAt moves tmp; ensure no leftover.
            try? FileManager.default.removeItem(at: tmp)
        } else {
            try FileManager.default.moveItem(at: tmp, to: pointerFile)
        }
    }

    static var resources: URL {
        // Dev (swift run) falls back to the repo layout next to the package.
        if let r = Bundle.main.resourceURL ??
            Bundle.main.bundleURL.appendingPathComponent("Contents/Resources")
            as URL?,
           FileManager.default.fileExists(
               atPath: r.appendingPathComponent("server").path) {
            return r
        }
        let here = URL(fileURLWithPath: #filePath)
        return here.deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }

    static var serverSrc: URL { resources.appendingPathComponent("server/src") }
    static var scripts: URL { resources.appendingPathComponent("scripts") }
    static var bundledCLI: URL { resources.appendingPathComponent("cli/3dfm") }
    static var tokenFile: URL { dataDir.appendingPathComponent("server.token") }
    static var probesFile: URL { dataDir.appendingPathComponent("probes.json") }
    static var settingsFile: URL { dataDir.appendingPathComponent("settings.json") }
}
