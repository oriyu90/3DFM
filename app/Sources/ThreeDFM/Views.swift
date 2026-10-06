import AppKit
import SwiftUI

// MARK: - Primary window: sidebar + queue/history + detail

enum QueueSection: String, Hashable {
    case queue, history
}

struct JobsView: View {
    @EnvironmentObject var backend: Backend
    @State private var section: QueueSection? = .queue
    @State private var selection: String?
    @State private var detail: Job?
    @State private var logText = ""
    @State private var filter = "all"

    var body: some View {
        NavigationSplitView {
            // HIG sidebars.md: familiar SF Symbols, accent default,
            // at most two levels, badges for counts.
            List(selection: $section) {
                Label("キュー", systemImage: "tray.fill")
                    .tag(QueueSection.queue)
                    .badge(backend.queuedJobs.count)
                Label("履歴", systemImage: "clock.fill")
                    .tag(QueueSection.history)
                    .badge(backend.historyJobs.count)
            }
            .navigationTitle("3DFM")
        } content: {
            List(jobsInScope, selection: $selection) { j in
                VStack(alignment: .leading) {
                    HStack {
                        Circle().fill(dot(j)).frame(width: 8, height: 8)
                        Text(j.name).lineLimit(1)
                        Spacer()
                        Text(stateLabel(j)).font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    ProgressView(value: j.progress, total: 100)
                        .progressViewStyle(.linear)
                    Text("\(j.stage) \(Int(j.progress))%")
                        .font(.caption).foregroundStyle(.secondary)
                }
                .padding(.vertical, 2)
            }
            .navigationTitle(section == .history ? "履歴" : "キュー")
            .navigationSubtitle(subtitle)
            .overlay {
                if jobsInScope.isEmpty {
                    ContentUnavailableView {
                        Label(section == .history ? "履歴は空です" : "キューは空です",
                              systemImage: section == .history
                                ? "clock.fill" : "tray.fill")
                    } description: {
                        Text(section == .history
                            ? "完了した生成がここに表示されます"
                            : "画像を選ぶと3Dモデルを生成できます")
                    } actions: {
                        if section != .history {
                            Button("新規生成…") { backend.showNewJob = true }
                        }
                    }
                }
            }
        } detail: {
            if let j = detail {
                JobDetailView(job: j, logText: logText)
            } else {
                ContentUnavailableView("ジョブを選択",
                    systemImage: "cube.fill",
                    description: Text("左の一覧から確認するジョブを選んでください"))
                    .foregroundStyle(.secondary)
            }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("新規生成", systemImage: "plus") {
                    backend.showNewJob = true
                }
                .keyboardShortcut("n", modifiers: .command)
            }
            ToolbarItem(placement: .principal) {
                Picker("", selection: $filter) {
                    Text("全て").tag("all")
                    Text("実行中").tag("running")
                    Text("待機").tag("queued")
                    Text("完了").tag("done")
                    Text("失敗").tag("failed")
                    Text("取消").tag("cancelled")
                }.pickerStyle(.segmented).labelsHidden()
            }
            // CLI parity: reorder is a first-class operation (POST /jobs/reorder).
            ToolbarItem(placement: .automatic) {
                Menu("並び替え", systemImage: "arrow.up.arrow.down") {
                    Button("選択を上へ") { moveSelection(by: -1) }
                    Button("選択を下へ") { moveSelection(by: 1) }
                }
                .disabled(section != .queue || selection == nil)
            }
        }
        .sheet(isPresented: $backend.showNewJob) {
            NewJobView()
                .environmentObject(backend)
                .frame(width: 460, height: 560)
        }
        .onChange(of: selection) { _, id in
            guard let id else { detail = nil; return }
            Task {
                detail = await backend.job(id: id)
                logText = await backend.jobLog(id: id)
            }
        }
        .onChange(of: section) { selection = nil; detail = nil }
        .task {
            // live-refresh the open detail; back off when idle so an
            // empty queue does not spin the CPU or hold memory.
            while !Task.isCancelled {
                let idle = backend.activeJob == nil && selection == nil
                try? await Task.sleep(for: .seconds(idle ? 3 : 1))
                await backend.refreshJobsQuiet()
                if let id = selection {
                    detail = await backend.job(id: id)
                    logText = await backend.jobLog(id: id)
                }
            }
        }
    }

    private func moveSelection(by delta: Int) {
        guard let sel = selection else { return }
        var ids = jobsInScope.map(\.id)
        guard let idx = ids.firstIndex(of: sel) else { return }
        let dst = idx + delta
        guard dst >= 0 && dst < ids.count else { return }
        ids.swapAt(idx, dst)
        // Preserve global queue order: merge moved ids back into the
        // full queued list order for the server.
        let allQueued = backend.queuedJobs.map(\.id)
        var order = allQueued
        // Apply the local swap to the global order when both ids are queued.
        if let a = order.firstIndex(of: sel),
           let b = order.firstIndex(of: ids[dst == idx ? idx : dst]) {
            order.swapAt(a, b)
        } else {
            order = ids
        }
        Task { _ = await backend.reorder(ids: order) }
    }

    private var base: [Job] {
        section == .history ? backend.historyJobs : backend.queuedJobs
    }

    private var jobsInScope: [Job] {
        filter == "all" ? base : base.filter { $0.state == filter }
    }

    private var subtitle: String {
        let n = jobsInScope.count
        if section == .history { return "\(n)件の履歴" }
        if let a = backend.activeJob { return "\(a.stage) \(Int(a.progress))%" }
        return n == 0 ? "待機中" : "\(n)件待機中"
    }

    private func stateLabel(_ j: Job) -> String {
        switch j.state {
        case "done": "完了"
        case "running": "生成中"
        case "failed": "失敗"
        case "cancelled": "キャンセル済み"
        default: "待機中"
        }
    }

    private func dot(_ j: Job) -> Color {
        switch j.state {
        case "done": .green
        case "running": .blue
        case "failed": .red
        case "cancelled": .gray
        default: .orange
        }
    }
}

struct JobDetailView: View {
    @EnvironmentObject var backend: Backend
    var job: Job
    var logText: String
    @State private var artifacts: [Artifact] = []
    @State private var busy = false
    @State private var notice = ""

    private var detailLine: String {
        var s = "mode=\(job.mode) stage=\(job.stage) 進捗=\(Int(job.progress))%"
        if let eta = job.eta_s {
            s += String(format: " 残り約%.0fs", eta)
        }
        return s
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(job.name).font(.title2)
                Spacer()
                Text(job.state).font(.headline)
            }
            Text(verbatim: detailLine)
                .font(.callout).foregroundStyle(.secondary)
            ProgressView(value: job.progress, total: 100)
            if !job.error.isEmpty {
                Text(job.error).foregroundStyle(.red).font(.callout)
                    .lineLimit(4)
            }
            HStack {
                if job.state == "running" || job.state == "queued" {
                    Button("キャンセル") {
                        Task { await backend.cancel(id: job.id) }
                    }
                    if job.state == "running" {
                        Button("強制終了") {
                            Task { await backend.cancel(id: job.id, force: true) }
                        }
                    }
                }
                if job.state == "failed" || job.state == "cancelled"
                    || job.state == "done" {
                    Button("再実行") {
                        Task { await backend.retry(id: job.id) }
                    }
                    Button("削除") {
                        busy = true
                        Task {
                            let ok = await backend.deleteHistory(id: job.id)
                            notice = ok ? "削除しました" : "削除に失敗しました"
                            busy = false
                        }
                    }.disabled(busy)
                }
                Button("フォルダを開く") { openJobDir() }
                Spacer()
            }
            if !notice.isEmpty {
                Text(notice).font(.caption).foregroundStyle(.secondary)
            }
            // CLI parity: artifacts list + per-file download (GET /jobs/{id}/file).
            if !artifacts.isEmpty {
                Text("成果物 (\(artifacts.count))").font(.headline)
                ForEach(artifacts, id: \.path) { a in
                    HStack {
                        Text(a.path).font(.caption).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Text("\(a.size / 1024) KB").font(.caption).foregroundStyle(.secondary)
                        Button("保存…") { saveArtifact(a) }
                            .font(.caption)
                    }
                }
            }
            Text("ログ").font(.headline)
            ScrollView {
                Text(logText.isEmpty ? "(ログなし)" : logText)
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .cornerRadius(6)
        }
        .padding(16)
        .task { artifacts = await backend.artifacts(id: job.id) }
    }

    private func openJobDir() {
        let dir = Paths.dataDir.appendingPathComponent("jobs/\(job.id)")
        NSWorkspace.shared.open(dir)
    }

    private func saveArtifact(_ a: Artifact) {
        Task {
            guard let data = await backend.artifactData(jobId: job.id, path: a.path) else {
                notice = "ダウンロードに失敗: \(a.path)"
                return
            }
            let panel = NSSavePanel()
            panel.nameFieldStringValue = URL(fileURLWithPath: a.path).lastPathComponent
            NSApp.activate(ignoringOtherApps: true)
            if panel.runModal() == .OK, let url = panel.url {
                do {
                    try data.write(to: url)
                    notice = "保存しました: \(url.lastPathComponent)"
                } catch {
                    notice = "保存に失敗: \(error.localizedDescription)"
                }
            }
        }
    }
}

// MARK: - Setup wizard: output folder + ONE button

struct SetupWizardView: View {
    @EnvironmentObject var backend: Backend
    @Environment(\.dismiss) private var dismiss
    @State private var outputDir =
        (NSSearchPathForDirectoriesInDomains(.picturesDirectory,
                                             .userDomainMask, true).first
            .map { $0 + "/3DFM" }) ?? "~/Pictures/3DFM"
    @State private var dataDir = Paths.dataDir.path
    @State private var modelsDir = ""
    @State private var tier = "normal"
    @State private var hfToken = HFTokenStore.load()
    @State private var rememberToken = true
    @StateObject private var runner = SetupRunner()

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("3DFM セットアップ / Setup").font(.title2)
            Text("出力フォルダを選んで「セットアップ開始」を押すだけです。ランタイム・AIモデルは自動で導入されます (初回は数十分かかることがあります)。 / Choose an output folder and press Start. Runtimes and AI models are installed automatically (first run may take tens of minutes).")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                TextField("出力フォルダ / Output folder", text: $outputDir)
                    .textFieldStyle(.roundedBorder)
                Button("選択… / Choose…", action: pickDir)
            }
            DisclosureGroup("保存場所の詳細 / Storage details (data & models)") {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        TextField("データフォルダ / Data folder (program files)", text: $dataDir)
                            .textFieldStyle(.roundedBorder)
                        Button("選択…", action: pickDataDir)
                    }
                    HStack {
                        TextField("モデルフォルダ / Models folder (空=既定 / empty=default)", text: $modelsDir)
                            .textFieldStyle(.roundedBorder)
                        Button("選択…", action: pickModelsDir)
                        Button("既定 / Default") { modelsDir = "" }
                    }
                    Text("データフォルダには venv・ランタイム・ジョブDBが入ります（venvs は絶対パスのためデータフォルダ内に固定）。モデル（約16〜60GB）のみ外付けSSD等に分離できます。 / Data folder holds venvs, runtimes and job DB (venvs stay inside it). Only models (~16–60 GB) can be separated to an external SSD.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.font(.callout)
            Picker("モデル / Models", selection: $tier) {
                Text("普通 (~16GB)").tag("normal")
                Text("人物込み (~35GB)").tag("human")
                Text("フル (humanと同等)").tag("full")
            }.pickerStyle(.segmented)
            Text("40GB+メモリのMacでは human/full を推奨。CLI `3dfm models ensure --tier human` と同一内容です。 / 40 GB+ Macs should use human/full. Same as CLI `3dfm models ensure --tier human`.")
                .font(.caption).foregroundStyle(.secondary)
            DisclosureGroup("Hugging Face トークン (ゲート付きモデル用・任意)") {
                SecureField("hf_... (同意済みトークン)", text: $hfToken)
                    .textFieldStyle(.roundedBorder)
                Toggle("キーチェーンに保存 (次回以降も自動使用)", isOn: $rememberToken)
                    .font(.callout)
                Text("背景除去などのゲート付きモデルに必要です。トークンはHugging Faceで発行できます。利用規約の詳細は設定＞モデルで確認できます。")
                    .font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 12) {
                    Link("トークン発行 (huggingface.co/settings/tokens)",
                         destination: URL(string: "https://huggingface.co/settings/tokens")!)
                }.font(.caption)
            }.font(.callout)
            if runner.running || runner.done {
                ProgressView(value: runner.progress, total: 100)
                Text(runner.phaseMsg).font(.caption).foregroundStyle(.secondary)
            }
            if !runner.failed.isEmpty {
                Text(runner.failed).foregroundStyle(.red).font(.callout)
            }
            Spacer()
            HStack {
                Spacer()
                if runner.done {
                    Button("閉じる / Close") {
                        backend.needsSetup = false
                        backend.launchServer()
                        Task { await backend.waitForHealth() }
                        dismiss()
                    }.keyboardShortcut(.defaultAction)
                } else {
                    Button("セットアップ開始 / Start setup") {
                        let tok = hfToken.trimmingCharacters(in: .whitespacesAndNewlines)
                        if rememberToken {
                            HFTokenStore.save(tok)
                        } else if !tok.isEmpty {
                            HFTokenStore.delete()
                        }
                        // Persist the chosen data location before setup so
                        // the server and CLI agree (UserDefaults + pointer).
                        let d = dataDir.trimmingCharacters(in: .whitespacesAndNewlines)
                        if !d.isEmpty && d != Paths.defaultDataDir.path {
                            try? Paths.setCustomDataDir(d)
                        } else if d.isEmpty || d == Paths.defaultDataDir.path {
                            try? Paths.setCustomDataDir("")
                        }
                        runner.start(dataDir: d.isEmpty ? Paths.dataDir.path : d,
                                     modelsDir: modelsDir.trimmingCharacters(in: .whitespacesAndNewlines),
                                     outputDir: outputDir, tier: tier,
                                     hfToken: tok)
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(runner.running)
                }
            }
        }
        .padding(20)
    }

    private func pickDir() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            outputDir = url.path
        }
    }

    private func pickDataDir() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            dataDir = url.path
        }
    }

    private func pickModelsDir() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            modelsDir = url.path
        }
    }

}

// MARK: - Setup runner (owns the setup.sh child process)
@MainActor
final class SetupRunner: ObservableObject {
    @Published var running = false
    @Published var progress = 0.0
    @Published var phaseMsg = ""
    @Published var done = false
    @Published var failed = ""

    func start(dataDir: String, modelsDir: String, outputDir: String, tier: String, hfToken: String) {
        running = true
        failed = ""
        done = false
        // Pin this instance for the detached reader below. init() also
        // sets it, but start() must re-pin: a recreated View may have
        // replaced the registry with a newer runner.
        SetupRunner.shared = self
        if !hfToken.isEmpty { HFTokenStore.save(hfToken) }
        Task.detached {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/bash")
            let script = Paths.scripts.appendingPathComponent("setup.sh").path
            var args = [script,
                        "--data-dir", (dataDir as NSString).expandingTildeInPath,
                        "--output-dir",
                        (outputDir as NSString).expandingTildeInPath,
                        "--models", tier]
            let m = modelsDir.trimmingCharacters(in: .whitespacesAndNewlines)
            if !m.isEmpty {
                args += ["--models-dir", (m as NSString).expandingTildeInPath]
            }
            proc.arguments = args
            var env = ProcessInfo.processInfo.environment
            let tok = hfToken.isEmpty ? HFTokenStore.load() : hfToken
            if !tok.isEmpty { env["HF_TOKEN"] = tok }
            proc.environment = env
            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe
            do { try proc.run() } catch {
                await MainActor.run {
                    SetupRunner.shared?.fail("開始できません: \(error.localizedDescription)")
                }
                return
            }
            let handle = pipe.fileHandleForReading
            var buf = Data()
            while proc.isRunning {
                if let chunk = try? handle.availableData, !chunk.isEmpty {
                    buf.append(chunk)
                    while let nl = buf.firstIndex(of: 0x0A) {
                        let line = Data(buf[..<nl])
                        buf.removeSubrange(...nl)
                        await SetupRunner.emit(line: line)
                    }
                } else {
                    try? await Task.sleep(for: .milliseconds(200))
                }
            }
            if let rest = try? handle.readToEnd(), !rest.isEmpty {
                buf.append(rest)
                for part in buf.split(separator: 0x0A) {
                    await SetupRunner.emit(line: Data(part))
                }
            }
            proc.waitUntilExit()
            await MainActor.run {
                guard let r = SetupRunner.shared else { return }
                r.running = false
                if proc.terminationStatus == 0 {
                    r.done = true
                    r.progress = 100
                    r.phaseMsg = "完了"
                } else if r.failed.isEmpty {
                    r.failed = "セットアップが異常終了しました (code \(proc.terminationStatus))。ログ: ~/Library/Application Support/3DFM/logs/setup.log"
                }
                if SetupRunner.shared === r {
                    SetupRunner.shared = nil
                }
            }
        }
    }

    // The detached task hops back through this registry because a struct
    // View cannot be mutated from outside.
    private static weak var shared: SetupRunner?

    private func fail(_ msg: String) {
        failed = msg
        running = false
    }

    private static func emit(line: Data) async {
        guard let r = shared,
              let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any]
        else { return }
        await MainActor.run {
            if obj["type"] as? String == "progress" {
                let step = (obj["step"] as? NSNumber)?.doubleValue ?? 0
                let steps = (obj["steps"] as? NSNumber)?.doubleValue ?? 8
                r.progress = steps > 0 ? step / steps * 100 : 0
                let phase = (obj["phase"] as? String).map { " [\($0)]" } ?? ""
                r.phaseMsg = ((obj["msg"] as? String) ?? "") + phase
            } else if obj["type"] as? String == "done",
                      (obj["ok"] as? Bool) == false {
                r.failed = (obj["error"] as? String) ?? "失敗"
            }
        }
    }

    init() { SetupRunner.shared = self }
}

// MARK: - Settings panes (HIG settings.md › Desktop: toolbar panes,
// title follows the active pane — handled by the Settings scene)

struct SettingsPanes: View {
    var body: some View {
        TabView {
            GeneralSettingsPane()
                .tabItem { Label("一般 / General", systemImage: "gear") }
            StorageSettingsPane()
                .tabItem { Label("ストレージ / Storage", systemImage: "externaldrive.fill") }
            ModelsSettingsPane()
                .tabItem { Label("モデル / Models", systemImage: "cube.fill") }
        }
        .frame(width: 620, height: 520)
    }
}

struct GeneralSettingsPane: View {
    @EnvironmentObject var backend: Backend
    // HIG (the-menu-bar › menu bar extras): people decide.
    @AppStorage("showMenuBarExtra") private var showMenuBarExtra = true
    @State private var outputDir = ""
    @State private var texture = 2048
    @State private var pipeline = "512->1024"
    @State private var memCap = "40"
    @State private var memMinFree = "12"
    @State private var stallTimeout = "1800"
    @State private var cancelGrace = "10"
    @State private var idleGc = "60"
    @State private var retention = "0"
    @State private var port = "44931"
    @State private var notify = true
    @State private var saved = ""

    var body: some View {
        Form {
            TextField("出力フォルダ:", text: $outputDir)
            HStack {
                Spacer()
                Button("選択…", action: pickDir)
            }
            Picker("既定テクスチャ:", selection: $texture) {
                Text("1024").tag(1024)
                Text("2048").tag(2048)
                Text("4096").tag(4096)
            }.pickerStyle(.segmented)
            Picker("既定パイプライン:", selection: $pipeline) {
                Text("512 (速い)").tag("512")
                Text("512→1024 (標準)").tag("512->1024")
                Text("512→1536 (最高)").tag("512->1536")
            }
            TextField("メモリ上限 RSS (GB):", text: $memCap)
            TextField("開始に必要な空き (GB):", text: $memMinFree)
            TextField("停滞タイムアウト (秒):", text: $stallTimeout)
            TextField("キャンセル猶予 (秒):", text: $cancelGrace)
            TextField("待機時GC間隔 (秒, 0=無効):", text: $idleGc)
            TextField("履歴保持 (日, 0=無期限):", text: $retention)
            TextField("ポート:", text: $port)
            Toggle("完了通知", isOn: $notify)
            Toggle("メニューバーに表示", isOn: $showMenuBarExtra)
            HStack {
                Button("保存") { save() }
                Button("再読込") { Task { await load() } }
                if !saved.isEmpty {
                    Text(saved).foregroundStyle(.secondary)
                }
            }
            Text("CLI `3dfm settings set` と同一キーです。")
                .font(.caption).foregroundStyle(.secondary)
        }
        .formStyle(.grouped)
        .padding(18)
        .task { await load() }
    }

    private func load() async {
        if let s = await backend.settings() {
            outputDir = (s["output_dir"] as? String) ?? ""
            texture = (s["texture_default"] as? NSNumber)?.intValue ?? 2048
            pipeline = (s["pipeline_default"] as? String) ?? "512->1024"
            memCap = numStr(s["mem_cap_gb"], fallback: "40")
            memMinFree = numStr(s["mem_min_free_gb"], fallback: "12")
            stallTimeout = numStr(s["stall_timeout_s"], fallback: "1800")
            cancelGrace = numStr(s["cancel_grace_s"], fallback: "10")
            idleGc = numStr(s["idle_gc_s"], fallback: "60")
            retention = numStr(s["retention_days"], fallback: "0")
            port = numStr(s["port"], fallback: "44931")
            notify = (s["notify"] as? Bool) ?? true
        }
    }

    private func numStr(_ v: Any?, fallback: String) -> String {
        if let n = v as? NSNumber { return n.stringValue }
        return fallback
    }

    private func pickDir() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            outputDir = url.path
        }
    }

    private func save() {
        Task {
            let ok = await backend.saveSettings([
                "output_dir": outputDir,
                "texture_default": texture,
                "pipeline_default": pipeline,
                "mem_cap_gb": Double(memCap) ?? 40,
                "mem_min_free_gb": Double(memMinFree) ?? 12,
                "stall_timeout_s": Double(stallTimeout) ?? 1800,
                "cancel_grace_s": Double(cancelGrace) ?? 10,
                "idle_gc_s": Double(idleGc) ?? 60,
                "retention_days": Double(retention) ?? 0,
                "port": Int(port) ?? 44931,
                "notify": notify,
            ])
            saved = ok ? "保存しました / Saved" : "保存に失敗しました (値をCLI `3dfm settings get` と比較してください) / Save failed (compare with CLI `3dfm settings get`)"
        }
    }
}

// MARK: - Storage locations (data + models folders)

struct StorageSettingsPane: View {
    @EnvironmentObject var backend: Backend
    @State private var info: StorageInfo?
    @State private var dataDir = ""
    @State private var modelsDir = ""
    @State private var moveDataFiles = true
    @State private var moveModelFiles = true
    @State private var busy = false
    @State private var message = ""
    @State private var isDefaultData = true
    @State private var isDefaultModels = true

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("プログラムファイルの保存場所 / Where program files live")
                .font(.headline)
            Text("データフォルダには venv・ランタイム・ジョブDBが入ります（venvs は絶対パスのため移動はデータフォルダごと）。モデル（約16〜60GB）のみ外付けSSD等へ分離できます。変更前にキューを空にしてください。 / Data folder holds venvs, runtimes and the job DB (venvs stay inside it). Only models (~16–60 GB) can live on an external SSD. Empty the queue before changing locations.")
                .font(.callout).foregroundStyle(.secondary)
            if let info {
                HStack {
                    Text("使用量 / Usage:").font(.caption)
                    Text("データ \(String(format: "%.1f", info.data_size_gb ?? 0)) GB / Data \(String(format: "%.1f", info.data_size_gb ?? 0)) GB")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("モデル \(String(format: "%.1f", info.models_size_gb ?? 0)) GB / Models \(String(format: "%.1f", info.models_size_gb ?? 0)) GB")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("空き \(String(format: "%.1f", info.free_gb ?? 0)) GB / Free \(String(format: "%.1f", info.free_gb ?? 0)) GB")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Divider()
            Text("データフォルダ / Data folder (program files root)").font(.callout)
            HStack {
                TextField(Paths.defaultDataDir.path, text: $dataDir)
                    .textFieldStyle(.roundedBorder)
                Button("選択… / Choose…", action: pickData)
                    .disabled(busy)
                Button("既定に戻す / Reset", action: resetData)
                    .disabled(busy)
            }
            .font(.callout)
            Toggle("既存データを移動する / Move existing data", isOn: $moveDataFiles)
                .font(.callout)
            HStack {
                Button(busy ? "適用中… / Applying…" : "データフォルダを適用・再起動 / Apply data folder + restart") {
                    applyData()
                }
                .disabled(busy || dataDir.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Button("開く / Open") {
                    NSWorkspace.shared.open(URL(fileURLWithPath: dataDir.isEmpty ? Paths.dataDir.path : dataDir))
                }
                .disabled(busy)
            }
            .font(.callout)
            Divider()
            Text("モデルフォルダ / Models folder (AI weights, 空=既定 / empty=default)").font(.callout)
            HStack {
                TextField("<既定> <data>/models / <default>", text: $modelsDir)
                    .textFieldStyle(.roundedBorder)
                Button("選択… / Choose…", action: pickModels)
                    .disabled(busy)
                Button("既定に戻す / Reset", action: resetModels)
                    .disabled(busy)
            }
            .font(.callout)
            Toggle("既存モデルを移動する / Move existing models (off=切替のみ / pointer-only)", isOn: $moveModelFiles)
                .font(.callout)
            HStack {
                Button(busy ? "適用中… / Applying…" : "モデルフォルダを適用 / Apply models folder") {
                    applyModels()
                }
                .disabled(busy)
                Button("開く / Open") {
                    let p = modelsDir.trimmingCharacters(in: .whitespacesAndNewlines)
                    NSWorkspace.shared.open(URL(fileURLWithPath: p.isEmpty ? (info?.models_dir ?? Paths.dataDir.appendingPathComponent("models").path) : p))
                }
                .disabled(busy)
            }
            .font(.callout)
            if !message.isEmpty {
                Text(message).font(.callout).foregroundStyle(.secondary).lineLimit(4)
            }
            Spacer()
            Text("CLI と同一操作: `3dfm storage status` / `3dfm storage move-models <path>` / `3dfm storage set-data-dir <path> --move` / Same operations as CLI.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(18)
        .task { await refresh() }
    }

    private func refresh() async {
        if let st = await backend.storage() {
            info = st
            dataDir = st.data_dir
            let cfg = (st.configured_models_dir ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            modelsDir = cfg
            isDefaultData = st.is_default_data ?? true
            isDefaultModels = st.is_default_models ?? true
        } else {
            dataDir = Paths.dataDir.path
            modelsDir = ""
        }
    }

    private func pickData() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            dataDir = url.path
        }
    }

    private func pickModels() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            modelsDir = url.path
        }
    }

    private func resetData() {
        dataDir = Paths.defaultDataDir.path
        moveDataFiles = false
        Task {
            busy = true
            message = "既定に戻しています… / Reverting to default…"
            let (ok, msg) = await backend.setDataDirAndRestart(path: "", moveExisting: false)
            message = msg
            busy = false
            if ok { await refresh() }
        }
    }

    private func resetModels() {
        modelsDir = ""
        Task {
            busy = true
            message = "既定に戻しています… / Reverting to default…"
            let (_, msg) = await backend.moveModels(path: "", moveFiles: false)
            message = msg
            busy = false
            await refresh()
        }
    }

    private func applyData() {
        let target = dataDir.trimmingCharacters(in: .whitespacesAndNewlines)
        if target.isEmpty {
            message = "データフォルダを入力してください / Enter a data folder"
            return
        }
        busy = true
        message = "切り替え中…（サーバー再起動あり） / Switching… (server restarts)"
        Task {
            let (ok, msg) = await backend.setDataDirAndRestart(path: target, moveExisting: moveDataFiles)
            message = msg
            busy = false
            if ok { await refresh() }
            _ = ok
        }
    }

    private func applyModels() {
        let target = modelsDir.trimmingCharacters(in: .whitespacesAndNewlines)
        busy = true
        message = "モデルフォルダを切り替え中…（数分かかることがあります） / Switching models folder… (may take minutes)"
        Task {
            let (ok, msg) = await backend.moveModels(path: target, moveFiles: moveModelFiles)
            message = msg
            busy = false
            await refresh()
            _ = ok
        }
    }
}

struct ModelsSettingsPane: View {
    @EnvironmentObject var backend: Backend
    @Environment(\.openWindow) private var openWindow
    @State private var models: [ModelInfo] = []
    @State private var runtimes: [String: String] = [:]
    @State private var hasToken = !HFTokenStore.load().isEmpty
    @State private var ensureMsg = ""
    @State private var ensureBusy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // HIG onboarding.md: licensing details live here, not in setup.
            Text("各モデルの利用規約への同意が必要な場合があります。ゲート付きモデルはHugging Faceで同意済みのトークンが必要です。")
                .font(.callout).foregroundStyle(.secondary)
            HStack(spacing: 12) {
                Link("DINOv3 利用規約",
                     destination: URL(string: "https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m")!)
                Link("RMBG-2.0 利用規約 (非商用)",
                     destination: URL(string: "https://huggingface.co/briaai/RMBG-2.0")!)
            }.font(.callout)
            Divider()
            ForEach(models, id: \.id) { m in
                HStack {
                    Circle().fill(m.present ? Color.green : Color.gray)
                        .frame(width: 8, height: 8)
                    Text(m.id)
                    Spacer()
                    Text(m.present ? String(format: "%.1f GB", m.size_gb) : "未導入")
                        .font(.caption).foregroundStyle(.secondary)
                }.font(.callout)
            }
            if !runtimes.isEmpty {
                Divider()
                ForEach(runtimes.sorted(by: { $0.key < $1.key }), id: \.key) { k, v in
                    HStack {
                        Text("runtime \(k)").font(.caption)
                        Spacer()
                        Text(v).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            if !ensureMsg.isEmpty {
                Text(ensureMsg).font(.caption).foregroundStyle(.secondary).lineLimit(3)
            }
            HStack {
                Button("不足モデルを取得 (normal)") {
                    ensure(tier: "normal")
                }.disabled(ensureBusy)
                Button("人物込み (human)") {
                    ensure(tier: "human")
                }.disabled(ensureBusy)
                Spacer()
            }
            .font(.callout)
            Text("CLI `3dfm models ensure --tier human` と同一操作です。 / Same as CLI `3dfm models ensure --tier human`.")
                .font(.caption).foregroundStyle(.secondary)
            Text("保存場所の変更は「ストレージ / Storage」タブで行います（モデル・データフォルダ）。 / Change locations in the “Storage” tab (models & data folders).")
                .font(.caption).foregroundStyle(.secondary)
            Spacer()
            HStack {
                Text(hasToken ? "HFトークン: 登録済み" : "HFトークン: 未登録")
                    .font(.callout).foregroundStyle(.secondary)
                if hasToken {
                    Button("クリア") {
                        HFTokenStore.delete()
                        hasToken = false
                    }
                }
                Spacer()
            }
            HStack {
                Button("セットアップを再実行") {
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "setup")
                }
                Spacer()
                Button("データを開く") {
                    NSWorkspace.shared.open(Paths.dataDir)
                }
            }
        }
        .padding(18)
        .task { await refresh() }
    }

    private func refresh() async {
        if let st = await backend.models() {
            models = st.models
            var r: [String: String] = [:]
            for (k, v) in st.runtimes ?? [:] {
                r[k] = (v.present == true) ? "導入済み" : "未導入"
            }
            runtimes = r
        }
        hasToken = !HFTokenStore.load().isEmpty
    }

    private func ensure(tier: String) {
        ensureBusy = true
        ensureMsg = "取得中… (\(tier))"
        Task {
            let out = await backend.modelsEnsure(tier: tier)
            ensureMsg = String(out.prefix(800))
            ensureBusy = false
            await refresh()
        }
    }
}
