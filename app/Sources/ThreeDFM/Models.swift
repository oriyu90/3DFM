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

// MARK: - Paths

enum Paths {
    static var dataDir: URL {
        if let o = ProcessInfo.processInfo.environment["FM3D_DATA_DIR"],
           !o.isEmpty {
            return URL(fileURLWithPath: (o as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/3DFM")
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
