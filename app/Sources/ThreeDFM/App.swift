import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let backend = Backend()

    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            self.backend.start()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        Task { @MainActor in
            self.backend.stop()
        }
    }
}

@main
struct ThreeDFMApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @Environment(\.openWindow) private var openWindow
    /// HIG (the-menu-bar › menu bar extras): people decide.
    @AppStorage("showMenuBarExtra") private var showMenuBarExtra = true

    private var backend: Backend { delegate.backend}


    /// GUI check hook: `ThreeDFM --show jobs settings ...` opens the
    /// real views in dedicated windows (same code as the windows above).

    var body: some Scene {
        // HIG (the-menu-bar › menu bar extras): a menu, not a popover.
        // The queue/progress UI lives in the primary window; the menu
        // carries status plus the handful of frequent commands.
        MenuBarExtra("3DFM", systemImage: "cube.fill",
                     isInserted: $showMenuBarExtra) {
            MenuExtraView()
                .environmentObject(backend)
        }
        .menuBarExtraStyle(.menu)

        // Primary window (HIG windows.md): queue + detail + toolbar.
        Window("3DFM", id: "jobs") {
            JobsView()
                .environmentObject(backend)
                .frame(minWidth: 820, minHeight: 520)
        }
        .windowResizability(.contentMinSize)

        // Standard Settings scene (HIG settings.md › Desktop): ⌘, works,
        // panes switch via toolbar, title follows the active pane.
        Settings {
            SettingsPanes()
                .environmentObject(backend)
        }

        // Auxiliary first-run window (HIG onboarding.md): prerequisites
        // only — output folder + token. Licensing lives in Settings.
        Window("3DFM — セットアップ", id: "setup") {
            SetupWizardView()
                .environmentObject(backend)
                .frame(width: 520, height: 560)
        }
        .defaultPosition(.center)

    }
}

// MARK: - Menu bar menu (not a popover)

struct MenuExtraView: View {
    @EnvironmentObject var backend: Backend
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        if backend.needsSetup {
            Button("セットアップを開始…") {
                NSApp.activate(ignoringOtherApps: true)
                openWindow(id: "setup")
            }
            Text("初回のみ: ランタイムとAIモデルを自動導入します")
        } else {
            Text(backend.statusLine)
            if let active = backend.activeJob {
                Button("キャンセル: \(active.name)") {
                    Task { await backend.cancel(id: active.id) }
                }
            }
            if !backend.queuedJobs.isEmpty {
                Menu("キュー (\(backend.queuedJobs.count))") {
                    ForEach(backend.queuedJobs.prefix(6)) { j in
                        Text("\(j.name) — \(Int(j.progress))%")
                    }
                }
            }
            Divider()
            Button("新規生成…") {
                backend.showNewJob = true
                openWindow(id: "jobs")
            }
            .keyboardShortcut("n", modifiers: .command)
            Button("キューを開く") {
                NSApp.activate(ignoringOtherApps: true)
                openWindow(id: "jobs")
            }
            Button("設定…") {
                NSApp.activate(ignoringOtherApps: true)
                openSettings()
            }
            .keyboardShortcut(",", modifiers: .command)
        }
        Divider()
        Button("3DFMを終了") { NSApp.terminate(nil) }
            .keyboardShortcut("q", modifiers: .command)
    }
}

// MARK: - New job

struct NewJobView: View {
    @EnvironmentObject var backend: Backend
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var mode = "normal"
    @State private var files: [URL] = []
    @State private var seedText = ""
    @State private var texture = 2048
    @State private var pipeline = "512->1024"
    @State private var synthesize = false
    @State private var rembgText = ""
    @State private var decimationText = ""
    @State private var stepsText = ""
    @State private var mvPrompt = ""
    @State private var mvStepsText = ""
    @State private var mvResText = ""
    @State private var busy = false
    @State private var error = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("新規生成").font(.title2)
            Picker("モード", selection: $mode) {
                Text("普通 (1枚)").tag("normal")
                Text("人物 (1枚 or 6枚)").tag("human")
            }.pickerStyle(.segmented)
            TextField("名前", text: $name)
                .textFieldStyle(.roundedBorder)
            HStack {
                Text("画像 (\(files.count))").foregroundStyle(.secondary)
                Spacer()
                Button("選択…", action: pickFiles)
            }
            ForEach(files, id: \.self) { f in
                Text(f.lastPathComponent).font(.caption).lineLimit(1)
            }
            if mode == "human" && files.count == 1 {
                Toggle("正面1枚から残り5視点を合成 (MV-Adapter)", isOn: $synthesize)
            }
            DisclosureGroup("詳細設定 (CLIと同一)") {
                TextField("seed (空=ランダム)", text: $seedText)
                    .textFieldStyle(.roundedBorder)
                Picker("パイプライン", selection: $pipeline) {
                    Text("512 (速い)").tag("512")
                    Text("512→1024 (標準)").tag("512->1024")
                    Text("512→1536 (最高・32GB+推奨)").tag("512->1536")
                }
                Picker("テクスチャ", selection: $texture) {
                    Text("1024").tag(1024)
                    Text("2048").tag(2048)
                    Text("4096").tag(4096)
                }.pickerStyle(.segmented)
                TextField("背景除去しきい値 (空=既定 0.5/人物0.4)", text: $rembgText)
                    .textFieldStyle(.roundedBorder)
                TextField("decimation_target (空=既定)", text: $decimationText)
                    .textFieldStyle(.roundedBorder)
                TextField("拡散steps (空=既定)", text: $stepsText)
                    .textFieldStyle(.roundedBorder)
                if mode == "human" {
                    TextField("MVプロンプト (空=high quality)", text: $mvPrompt)
                        .textFieldStyle(.roundedBorder)
                    TextField("MV steps (空=50)", text: $mvStepsText)
                        .textFieldStyle(.roundedBorder)
                    TextField("MV解像度 (空=自動)", text: $mvResText)
                        .textFieldStyle(.roundedBorder)
                }
            }
            if !error.isEmpty {
                Text(error).foregroundStyle(.red).font(.callout)
            }
            Spacer()
            HStack {
                Spacer()
                Button("キャンセル") { dismiss() }
                Button("キューに追加") { submit() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || files.isEmpty)
            }
        }
        .padding(18)
    }

    private func pickFiles() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.png, .jpeg, .webP, .tiff, .bmp]
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK { files = panel.urls }
    }

    private func submit() {
        busy = true
        error = ""
        Task {
            do {
                let seed = Int(seedText.trimmingCharacters(in: .whitespaces))
                var extra: [String: Any] = [:]
                if let v = Double(rembgText.trimmingCharacters(in: .whitespaces)), !rembgText.isEmpty {
                    extra["rembg_threshold"] = v
                }
                if let v = Int(decimationText.trimmingCharacters(in: .whitespaces)), !decimationText.isEmpty {
                    extra["decimation_target"] = v
                }
                if let v = Int(stepsText.trimmingCharacters(in: .whitespaces)), !stepsText.isEmpty {
                    extra["steps"] = v
                }
                if !mvPrompt.trimmingCharacters(in: .whitespaces).isEmpty {
                    extra["mv_prompt"] = mvPrompt
                }
                if let v = Int(mvStepsText.trimmingCharacters(in: .whitespaces)), !mvStepsText.isEmpty {
                    extra["mv_steps"] = v
                }
                if let v = Int(mvResText.trimmingCharacters(in: .whitespaces)), !mvResText.isEmpty {
                    extra["mv_resolution"] = v
                }
                _ = try await backend.submit(
                    name: name.isEmpty ? "job" : name,
                    mode: mode, seed: seed, textureSize: texture,
                    pipeline: pipeline, synthesizeViews: synthesize,
                    files: files, extra: extra)
                dismiss()
            } catch {
                self.error = "投入に失敗: \(error.localizedDescription)"
                busy = false
            }
        }
    }
}
