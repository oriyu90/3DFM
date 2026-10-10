import Foundation
import UserNotifications

// MARK: - Server lifecycle + REST client (thin; never runs inference)

@MainActor
final class Backend: ObservableObject {
    @Published var serverUp = false
    @Published var health: Health?
    @Published var jobs: [Job] = []
    @Published var lastError = ""
    @Published var needsSetup = false
    /// Observed by the main window: opens 新規生成 as a sheet.
    @Published var showNewJob = false

    /// Sidebar sections (HIG: queue = active work, history = done work).
    var queuedJobs: [Job] {
        jobs.filter { $0.state == "queued" || $0.state == "running" }
    }
    var historyJobs: [Job] {
        jobs.filter { $0.state == "done" || $0.state == "failed"
            || $0.state == "cancelled" }
    }
    var activeJob: Job? {
        jobs.first { $0.state == "running" }
            ?? jobs.first { $0.state == "queued" }
    }
    var statusLine: String {
        if !serverUp { return needsSetup ? "未セットアップ / Not set up" : "起動中… / Starting…" }
        if let a = activeJob {
            return "\(a.name) — \(a.stage) \(Int(a.progress))%"
        }
        return "待機中 / Idle"
    }

    private var serverProc: Process?
    private var pollTask: Task<Void, Never>?
    private var port = 44931

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }

    var token: String {
        (try? String(contentsOf: Paths.tokenFile)
            .trimmingCharacters(in: .whitespacesAndNewlines)) ?? ""
    }

    // -- startup ------------------------------------------------------
    private var started = false

    func start() {
        guard !started else { return }
        started = true
        needsSetup = !FileManager.default.fileExists(
            atPath: Paths.probesFile.path)
        launchServer()
        pollTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                await self.refreshJobsQuiet()
                // Idle backoff: 1s while work exists, 3s when idle so an
                // idle menu-bar app holds ~no CPU and lets memory settle.
                let idle = await MainActor.run { self.activeJob == nil }
                try? await Task.sleep(for: .seconds(idle ? 3 : 1))
            }
        }
    }

    func stop() {
        pollTask?.cancel()
        serverProc?.terminate()
    }

    private func serverPython() -> String {
        let venvPy = Paths.dataDir.appendingPathComponent(
            "venvs/server/bin/python").path
        if FileManager.default.isExecutableFile(atPath: venvPy) { return venvPy }
        return "/usr/bin/python3"
    }

    func launchServer() {
        if serverProc?.isRunning == true { return }
        let venvPy = Paths.dataDir.appendingPathComponent(
            "venvs/server/bin/python").path
        if !FileManager.default.isExecutableFile(atPath: venvPy) {
            // Runtime not installed yet: the SetupWizard owns this path.
            // Do not spawn a doomed server with the system python.
            needsSetup = true
            lastError = "ランタイム未導入: セットアップを実行してください / Runtime not installed: run Setup"
            return
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: serverPython())
        proc.arguments = ["-m", "fm3d.main"]
        var env = ProcessInfo.processInfo.environment
        env["FM3D_DATA_DIR"] = Paths.dataDir.path
        env["PYTHONPATH"] = Paths.serverSrc.path +
            (env["PYTHONPATH"].map { ":" + $0 } ?? "")
        env["PYTHONUNBUFFERED"] = "1"
        // Gated-model auth: keychain (or caller env) flows to the server
        // and, by inheritance, to every worker it spawns.
        let hf = HFTokenStore.load()
        if !hf.isEmpty { env["HF_TOKEN"] = hf }
        proc.environment = env
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            serverProc = proc
        } catch {
            lastError = "サーバーを起動できません: \(error.localizedDescription) / Cannot start server: \(error.localizedDescription)"
            return
        }
        Task { [weak self] in await self?.waitForHealth() }
    }

    func waitForHealth() async {
        for p in 44931 ..< 44941 {
            if await ping(port: p) { return }
        }
        lastError = "サーバーに接続できません。セットアップが必要です。 / Cannot reach server. Setup is required."
    }

    private func ping(port p: Int) async -> Bool {
        guard let data = try? await getRaw(path: "http://127.0.0.1:\(p)/health",
                                           authed: false),
              let h = try? JSONDecoder().decode(Health.self, from: data),
              h.status == "ok"
        else { return false }
        port = p
        health = h
        serverUp = true
        return true
    }

    // -- REST ----------------------------------------------------------
    private func authed(_ req: inout URLRequest) {
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    }

    func getRaw(path: String, authed auth: Bool = true) async throws -> Data {
        var req = URLRequest(url: URL(string: path)!,
                             timeoutInterval: 20)
        if auth { authed(&req) }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw BackendError.http((resp as? HTTPURLResponse)?.statusCode ?? 0)
        }
        return data
    }

    func get<T: Decodable>(_ path: String) async throws -> T {
        let data = try await getRaw(path: baseURL.appendingPathComponent(path)
            .absoluteString)
        return try JSONDecoder().decode(T.self, from: data)
    }

    struct JobList: Decodable { var jobs: [Job] }
    struct JobLog: Decodable { var log: String }
    struct ArtifactList: Decodable { var artifacts: [Artifact] }

    func refreshJobsQuiet() async {
        guard !token.isEmpty else { return }
        if let list: JobList = try? await get("jobs") {
            let prev = Dictionary(uniqueKeysWithValues: jobs.map { ($0.id, $0.state) })
            jobs = list.jobs
            notifyTransitions(prev: prev, now: list.jobs)
        }
        if let data = try? await getRaw(path: baseURL
            .appendingPathComponent("health").absoluteString, authed: false),
           let h = try? JSONDecoder().decode(Health.self, from: data) {
            health = h
            serverUp = true
        }
    }

    private func notifyTransitions(prev: [String: String], now: [Job]) {
        for j in now where j.state == "done" || j.state == "failed" {
            if prev[j.id] == "running" {
                Notifier.shared.send(title: j.state == "done" ? "生成が完了しました / Generation done" : "生成に失敗しました / Generation failed",
                                     body: "\(j.name): \(j.state == "done" ? "成果物を確認できます / Check artifacts" : j.error)")
            }
        }
    }

    func job(id: String) async -> Job? {
        try? await get("jobs/\(id)")
    }

    func jobLog(id: String) async -> String {
        var comps = URLComponents(
            url: baseURL.appendingPathComponent("jobs/\(id)/log"),
            resolvingAgainstBaseURL: false)!
        comps.queryItems = [.init(name: "tail", value: "200")]
        guard let url = comps.url,
              let data = try? await getRaw(path: url.absoluteString)
        else { return "" }
        return (try? JSONDecoder().decode(JobLog.self, from: data))?.log ?? ""
    }

    func artifacts(id: String) async -> [Artifact] {
        (try? await get("jobs/\(id)/artifacts") as ArtifactList)?.artifacts ?? []
    }

    func cancel(id: String, force: Bool = false) async {
        var comps = URLComponents(url: baseURL.appendingPathComponent("jobs/\(id)"),
                                  resolvingAgainstBaseURL: false)!
        if force { comps.queryItems = [.init(name: "force", value: "true")] }
        var req = URLRequest(url: comps.url!, timeoutInterval: 20)
        req.httpMethod = "DELETE"
        authed(&req)
        _ = try? await URLSession.shared.data(for: req)
        await refreshJobsQuiet()
    }

    /// Delete finished history (done/failed/cancelled). Same DELETE
    /// endpoint as cancel; the server routes by state so GUI and CLI
    /// share one operation.
    func deleteHistory(id: String) async -> Bool {
        let comps = URLComponents(url: baseURL.appendingPathComponent("jobs/\(id)"),
                                  resolvingAgainstBaseURL: false)!
        // Percent-encode the id segment safely.
        guard let url = comps.url else { return false }
        var req = URLRequest(url: url, timeoutInterval: 20)
        req.httpMethod = "DELETE"
        authed(&req)
        guard let (_, resp) = try? await URLSession.shared.data(for: req) else {
            return false
        }
        let ok = (resp as? HTTPURLResponse)?.statusCode == 200
        await refreshJobsQuiet()
        return ok
    }

    func reorder(ids: [String]) async -> Bool {
        var req = URLRequest(url: baseURL.appendingPathComponent("jobs/reorder"),
                             timeoutInterval: 20)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authed(&req)
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["ids": ids])
        guard let (_, resp) = try? await URLSession.shared.data(for: req) else {
            return false
        }
        let ok = (resp as? HTTPURLResponse)?.statusCode == 200
        await refreshJobsQuiet()
        return ok
    }

    func modelsEnsure(tier: String) async -> String {
        var req = URLRequest(url: baseURL.appendingPathComponent("models/ensure"),
                             timeoutInterval: 3600)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authed(&req)
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["tier": tier])
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let s = String(data: data, encoding: .utf8) else {
            return "モデル取得の開始に失敗しました / Failed to start fetching models"
        }
        return s
    }

    func artifactData(jobId: String, path: String) async -> Data? {
        var comps = URLComponents(
            url: baseURL.appendingPathComponent("jobs/\(jobId)/file"),
            resolvingAgainstBaseURL: false)!
        comps.queryItems = [.init(name: "path", value: path)]
        guard let url = comps.url else { return nil }
        var req = URLRequest(url: url, timeoutInterval: 120)
        authed(&req)
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return data
    }

    func retry(id: String) async {
        var req = URLRequest(url: baseURL.appendingPathComponent("jobs/\(id)/retry"),
                             timeoutInterval: 30)
        req.httpMethod = "POST"
        authed(&req)
        _ = try? await URLSession.shared.data(for: req)
        await refreshJobsQuiet()
    }

    struct SubmitResult: Decodable { var id: String }

    func submit(name: String, mode: String, seed: Int?, textureSize: Int,
                pipeline: String, synthesizeViews: Bool,
                files: [URL],
                extra: [String: Any] = [:]) async throws -> String {
        let boundary = "3DFM-\(UUID().uuidString)"
        var body = Data()
        var spec: [String: Any] = [
            "mode": mode, "name": name,
            "texture_size": textureSize,
            "pipeline_type": pipeline, "synthesize_views": synthesizeViews,
        ]
        if let seed { spec["seed"] = seed } else { spec["seed"] = NSNull() }
        for (k, v) in extra { spec[k] = v }
        let specData = try JSONSerialization.data(withJSONObject: spec)
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"spec\"\r\n\r\n"
            .data(using: .utf8)!)
        body.append(specData)
        body.append("\r\n".data(using: .utf8)!)
        for f in files {
            let data = try Data(contentsOf: f)
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"images\"; filename=\"\(f.lastPathComponent)\"\r\n"
                .data(using: .utf8)!)
            body.append("Content-Type: application/octet-stream\r\n\r\n"
                .data(using: .utf8)!)
            body.append(data)
            body.append("\r\n".data(using: .utf8)!)
        }
        body.append("--\(boundary)--\r\n".data(using: .utf8)!)
        var req = URLRequest(url: baseURL.appendingPathComponent("jobs"),
                             timeoutInterval: 120)
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)",
                     forHTTPHeaderField: "Content-Type")
        authed(&req)
        let (data, resp) = try await URLSession.shared.upload(for: req, from: body)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            let msg = String(data: data, encoding: .utf8) ?? ""
            throw BackendError.submit(msg)
        }
        return try JSONDecoder().decode(SubmitResult.self, from: data).id
    }

    func models() async -> ModelsStatus? {
        try? await get("models/status")
    }

    func settings() async -> [String: Any]? {
        guard let data = try? await getRaw(path: baseURL
            .appendingPathComponent("settings").absoluteString) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    func saveSettings(_ patch: [String: Any]) async -> Bool {
        var req = URLRequest(url: baseURL.appendingPathComponent("settings"),
                             timeoutInterval: 20)
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authed(&req)
        req.httpBody = try? JSONSerialization.data(withJSONObject: patch)
        guard let (_, resp) = try? await URLSession.shared.data(for: req) else {
            return false
        }
        return (resp as? HTTPURLResponse)?.statusCode == 200
    }

    // -- Storage locations (Settings > Storage / 設定＞ストレージ) ----
    // Bilingual UI: every user-visible string carries JP + EN.

    func storage() async -> StorageInfo? {
        try? await get("storage")
    }

    /// Move model weights (server-side, no restart).
    /// - path: absolute folder, or "" to revert to default.
    /// - moveFiles: true = move existing weights; false = pointer-only.
    /// Returns (ok, bilingual message).
    func moveModels(path: String, moveFiles: Bool) async -> (Bool, String) {
        var req = URLRequest(url: baseURL.appendingPathComponent("storage/models/move"),
                             timeoutInterval: 3600)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authed(&req)
        req.httpBody = try? JSONSerialization.data(
            withJSONObject: ["path": path, "move_files": moveFiles])
        guard let (data, resp) = try? await URLSession.shared.data(for: req) else {
            return (false, "サーバーに接続できません / Cannot reach the server")
        }
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            var serverMsg: String? = nil
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                serverMsg = obj["detail"] as? String
            }
            if serverMsg == nil {
                serverMsg = String(data: data, encoding: .utf8)
            }
            return (false, serverMsg ?? "移動に失敗しました / Move failed")
        }
        await refreshJobsQuiet()
        if path.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return (true, "既定のモデルフォルダに戻しました / Reverted to the default models folder")
        }
        return (true, moveFiles
            ? "モデルを移動しました / Models moved"
            : "モデルフォルダを切り替えました（不足分はセットアップで取得） / Models folder switched (fetch missing via Setup)")
    }

    func validateDataDir(path: String) async -> String {
        var req = URLRequest(url: baseURL.appendingPathComponent("storage/data/validate"),
                             timeoutInterval: 30)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authed(&req)
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["path": path])
        guard let (data, resp) = try? await URLSession.shared.data(for: req) else {
            return "サーバーに接続できません / Cannot reach the server"
        }
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            var serverMsg: String? = nil
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                serverMsg = obj["detail"] as? String
            }
            if serverMsg == nil {
                serverMsg = String(data: data, encoding: .utf8)
            }
            return serverMsg ?? "検証に失敗しました / Validation failed"
        }
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let note = obj["note"] as? String {
            return note
        }
        return "OK"
    }

    /// Switch the data folder (program files root). Offline operation:
    /// stops the server, optionally moves existing data, persists the
    /// pointer, then restarts. Must be called with no queued/running jobs.
    /// Returns (ok, bilingual message). Never crashes: all errors are values.
    func setDataDirAndRestart(path: String, moveExisting: Bool) async -> (Bool, String) {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        if !queuedJobs.isEmpty || activeJob != nil {
            return (false, "キューにジョブがあるため変更できません（先に完了・削除してください） / Clear the queue before changing the data folder")
        }
        // Basic validation (server re-validates on next launch via probe).
        if !trimmed.isEmpty {
            if trimmed.contains("\0") {
                return (false, "無効なパスです / Invalid path")
            }
            let url = URL(fileURLWithPath: (trimmed as NSString).expandingTildeInPath)
            if !url.path.hasPrefix("/") {
                return (false, "絶対パスで指定してください / Use an absolute path")
            }
            if url.pathComponents.contains("..") {
                return (false, "'..'は使えません / '..' is not allowed")
            }
            for blocked in ["/System", "/Library", "/Applications", "/bin",
                            "/sbin", "/usr", "/etc", "/var", "/private"] {
                if url.path == blocked {
                    return (false, "システムフォルダには設定できません / System folders cannot be used")
                }
            }
            // External volumes must be mounted; otherwise mkdir would create
            // a fake mountpoint on the boot disk.
            let comps = url.pathComponents
            if comps.count >= 3 && comps[0] == "/" && comps[1] == "Volumes" {
                let vol = "/Volumes/" + comps[2]
                if !FileManager.default.fileExists(atPath: vol) {
                    return (false, "外付けボリュームがマウントされていません: \(vol)（先に接続してください） / External volume not mounted: \(vol) (connect the drive first)")
                }
            }
        }
        let oldDir = Paths.dataDir
        // Stop the server first (offline move: DB must be closed).
        pollTask?.cancel()
        serverProc?.terminate()
        // Give the child a moment to drop server.pid / jobs.db-WAL.
        try? await Task.sleep(for: .seconds(1.5))
        serverProc = nil
        serverUp = false
        let newURL: URL = trimmed.isEmpty
            ? Paths.defaultDataDir
            : URL(fileURLWithPath: (trimmed as NSString).expandingTildeInPath)
        do {
            try FileManager.default.createDirectory(
                at: newURL, withIntermediateDirectories: true)
            // Writability probe (create + delete, no data in RAM).
            let probe = newURL.appendingPathComponent(".3dfm-write-test")
            try "ok".write(to: probe, atomically: true, encoding: .utf8)
            try? FileManager.default.removeItem(at: probe)
        } catch {
            // Restart with the old location so the app never strands.
            launchServer()
            return (false, "フォルダに書き込めません: \(error.localizedDescription) / Folder is not writable: \(error.localizedDescription)")
        }
        if moveExisting && !trimmed.isEmpty {
            do {
                let entries = try FileManager.default.contentsOfDirectory(
                    at: oldDir, includingPropertiesForKeys: nil)
                // Refuse merging into a non-empty destination.
                let dstNames = Set((try? FileManager.default.contentsOfDirectory(
                    at: newURL, includingPropertiesForKeys: nil)
                    .map { $0.lastPathComponent }) ?? [])
                for e in entries {
                    if dstNames.contains(e.lastPathComponent) {
                        throw NSError(domain: "3DFM", code: 409, userInfo: [
                            NSLocalizedDescriptionKey:
                            "移動先に '\(e.lastPathComponent)' が既にあります / Destination already has '\(e.lastPathComponent)'"])
                    }
                }
                for e in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                    let dst = newURL.appendingPathComponent(e.lastPathComponent)
                    do {
                        try FileManager.default.moveItem(at: e, to: dst)
                    } catch {
                        // Cross-volume fallback: copy + delete (streams,
                        // never loads weights into RAM).
                        var isDir: ObjCBool = false
                        _ = FileManager.default.fileExists(atPath: e.path, isDirectory: &isDir)
                        if isDir.boolValue {
                            try FileManager.default.copyItem(at: e, to: dst)
                            try FileManager.default.removeItem(at: e)
                        } else {
                            try FileManager.default.copyItem(at: e, to: dst)
                            try FileManager.default.removeItem(at: e)
                        }
                    }
                }
            } catch {
                try? Paths.setCustomDataDir(trimmed)
                launchServer()
                Task { await self.refreshJobsQuiet() }
                return (false, "移動中に中断: \(error.localizedDescription)。移動済みは移動先に残ります / Move interrupted: \(error.localizedDescription). Already-moved items stay at the destination.")
            }
        }
        do {
            try Paths.setCustomDataDir(trimmed)
        } catch {
            launchServer()
            return (false, "保存に失敗しました: \(error.localizedDescription) / Cannot save: \(error.localizedDescription)")
        }
        // Restart against the new location.
        needsSetup = !FileManager.default.fileExists(atPath: Paths.probesFile.path)
        launchServer()
        pollTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                await self.refreshJobsQuiet()
                let idle = await MainActor.run { self.activeJob == nil }
                try? await Task.sleep(for: .seconds(idle ? 3 : 1))
            }
        }
        await waitForHealth()
        return (true, trimmed.isEmpty
            ? "既定のデータフォルダに戻しました（再起動済み） / Reverted to the default data folder (restarted)"
            : "データフォルダを切り替え再起動しました / Data folder switched and restarted")
    }
}

enum BackendError: Error {
    case http(Int)
    case submit(String)
}

// MARK: - Notifications (best effort; never fatal)

@MainActor
final class Notifier {
    static let shared = Notifier()
    private var asked = false

    func send(title: String, body: String) {
        Task {
            let center = UNUserNotificationCenter.current()
            if !asked {
                asked = true
                _ = try? await center.requestAuthorization(options: [.alert, .sound])
            }
            let settings = await center.notificationSettings()
            guard settings.authorizationStatus == .authorized else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            let req = UNNotificationRequest(identifier: UUID().uuidString,
                                            content: content, trigger: nil)
            try? await center.add(req)
        }
    }
}
