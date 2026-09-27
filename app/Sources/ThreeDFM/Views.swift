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
                }.pickerStyle(.segmented).labelsHidden()
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
            // live-refresh the open detail once per second
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                await backend.refreshJobsQuiet()
                if let id = selection {
                    detail = await backend.job(id: id)
                    logText = await backend.jobLog(id: id)
                }
            }
        }
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
                }
                if job.state == "failed" || job.state == "cancelled"
                    || job.state == "done" {
                    Button("再実行") {
                        Task { await backend.retry(id: job.id) }
                    }
                }
                Button("フォルダを開く") { openJobDir() }
                Spacer()
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
}

// MARK: - Setup wizard: output folder + ONE button

struct SetupWizardView: View {
    @EnvironmentObject var backend: Backend
    @Environment(\.dismiss) private var dismiss
    @State private var outputDir =
        (NSSearchPathForDirectoriesInDomains(.picturesDirectory,
                                             .userDomainMask, true).first
            .map { $0 + "/3DFM" }) ?? "~/Pictures/3DFM"
    @State private var tier = "normal"
    @State private var hfToken = HFTokenStore.load()
    @State private var rememberToken = true
    @StateObject private var runner = SetupRunner()

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("3DFM セットアップ").font(.title2)
            Text("出力フォルダを選んで「セットアップ開始」を押すだけです。ランタイム・AIモデルは自動で導入されます (初回は数十分かかることがあります)。")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                TextField("出力フォルダ", text: $outputDir)
                    .textFieldStyle(.roundedBorder)
                Button("選択…", action: pickDir)
            }
            Picker("モデル", selection: $tier) {
                Text("普通モード (~16GB)").tag("normal")
                Text("人物モード込み (~35GB)").tag("human")
            }.pickerStyle(.segmented)
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
                    Button("閉じる") {
                        backend.needsSetup = false
                        backend.launchServer()
                        Task { await backend.waitForHealth() }
                        dismiss()
                    }.keyboardShortcut(.defaultAction)
                } else {
                    Button("セットアップ開始") {
                        let tok = hfToken.trimmingCharacters(in: .whitespacesAndNewlines)
                        if rememberToken {
                            HFTokenStore.save(tok)
                        } else if !tok.isEmpty {
                            HFTokenStore.delete()
                        }
                        runner.start(outputDir: outputDir, tier: tier,
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

}

// MARK: - Setup runner (owns the setup.sh child process)
@MainActor
final class SetupRunner: ObservableObject {
    @Published var running = false
    @Published var progress = 0.0
    @Published var phaseMsg = ""
    @Published var done = false
    @Published var failed = ""

    func start(outputDir: String, tier: String, hfToken: String) {
        running = true
        failed = ""
        done = false
        if !hfToken.isEmpty { HFTokenStore.save(hfToken) }
        Task.detached {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/bash")
            let script = Paths.scripts.appendingPathComponent("setup.sh").path
            proc.arguments = [script,
                              "--data-dir", Paths.dataDir.path,
                              "--output-dir",
                              (outputDir as NSString).expandingTildeInPath,
                              "--models", tier]
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
                .tabItem { Label("一般", systemImage: "gear") }
            ModelsSettingsPane()
                .tabItem { Label("モデル", systemImage: "cube.fill") }
        }
        .frame(width: 580, height: 460)
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
            TextField("メモリ上限 (GB):", text: $memCap)
            Toggle("メニューバーに表示", isOn: $showMenuBarExtra)
            HStack {
                Button("保存") { save() }
                if !saved.isEmpty {
                    Text(saved).foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .padding(18)
        .task {
            if let s = await backend.settings() {
                outputDir = (s["output_dir"] as? String) ?? ""
                texture = (s["texture_default"] as? NSNumber)?.intValue ?? 2048
                pipeline = (s["pipeline_default"] as? String) ?? "512->1024"
                memCap = String((s["mem_cap_gb"] as? NSNumber)?.intValue ?? 40)
            }
        }
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
            ])
            saved = ok ? "保存しました" : "保存に失敗しました"
        }
    }
}

struct ModelsSettingsPane: View {
    @EnvironmentObject var backend: Backend
    @Environment(\.openWindow) private var openWindow
    @State private var models: [ModelInfo] = []
    @State private var hasToken = !HFTokenStore.load().isEmpty

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
        .task { models = await backend.models()?.models ?? [] }
    }
}
