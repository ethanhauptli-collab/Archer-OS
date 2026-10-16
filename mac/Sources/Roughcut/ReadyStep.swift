import RoughcutKit
import SwiftUI

/// The last step: the line as it will run, each part with a way back to change it, and the settings most cuts leave alone.
struct ReadyStep: View {
    @Environment(AppModel.self) private var model

    private struct Row: Identifiable {
        var id: String
        var step: FlowStep
        var station: LineStation
    }

    private var rows: [Row] {
        var list = [
            Row(id: "footage", step: .footage, station: .footage),
            Row(id: "brief", step: .brief, station: .brief),
        ]
        for part in model.flow.parts(for: model.options) {
            list.append(Row(id: part.rawValue, step: .part(part), station: .part(part)))
        }
        return list
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            StepHeader(title: "Ready to build", subtitle: "This is your line. Change a part, or send it down.")
            StepCard {
                ForEach(rows) { row in
                    HStack(spacing: 14) {
                        IconTile(symbol: row.station.symbol, tint: true, size: 36)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(title(of: row.station))
                                .font(.system(size: 15, weight: .semibold))
                            Text(BuildFlow.readyDetail(of: row.station, options: model.options, footage: model.footageSummary))
                                .font(.system(size: 13))
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 8)
                        Button("Edit") { model.go(to: row.step) }
                            .buttonStyle(.borderless)
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                    Divider()
                }
                HStack(spacing: 14) {
                    Text(BuildFlow.fineTuneSummary(for: model.options))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    Button("Fine-tune…") { model.go(to: .fineTune) }
                        .buttonStyle(.borderless)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(Color.secondary.opacity(0.06))
            }
            Text("Later runs on the same footage are much faster, because transcripts and clip notes are saved.")
                .font(.callout)
                .foregroundStyle(.secondary)
            ForEach(model.options.problems, id: \.self) { problem in
                Label(problem, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func title(of station: LineStation) -> String {
        switch station {
        case .part(let part): return part.title
        default: return station.label
        }
    }
}

/// Fine-tune: the settings most cuts leave alone, in the controls the long form used to carry.
struct FineTuneStep: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        VStack(alignment: .leading, spacing: 18) {
            StepHeader(title: "Fine-tune", subtitle: "The defaults suit most cuts. Change only what you need.")

            Text("Planning")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.secondary)
                .padding(.bottom, -10)
            StepCard {
                SettingRow("Who plans the edit") {
                    Picker("Who plans the edit", selection: Binding(
                        get: { model.options.provider },
                        set: { model.options.choose(provider: $0) }  // None also turns the graphics off
                    )) {
                        ForEach(ModelProvider.allCases) { provider in
                            Text(provider.title).tag(provider)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 300)
                }
                if model.options.provider.usesClaudeModels {
                    Divider()
                    SettingRow("Model") {
                        Picker("Model", selection: $model.options.model) {
                            ForEach(ClaudeModelChoice.planners) { choice in
                                Text(choice.title).tag(choice.id)
                            }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 300)
                    }
                } else if model.options.provider != .none {
                    Divider()
                    SettingRow("Model") {
                        TextField("Model", text: $model.options.model, prompt: Text(model.options.provider.modelPlaceholder))
                            .labelsHidden()
                            .frame(maxWidth: 300)
                    }
                }
                if model.options.provider.usesBaseURL {
                    Divider()
                    SettingRow("Server URL") {
                        TextField("Server URL", text: $model.options.baseURL, prompt: Text("http://localhost:1234/v1"))
                            .labelsHidden()
                            .frame(maxWidth: 300)
                    }
                }
                if model.options.provider.usesClaudeModels {
                    Divider()
                    SettingRow("Effort") {
                        Picker("Effort", selection: $model.options.effort) {
                            ForEach(Effort.allCases) { effort in
                                Text(effort.title).tag(effort)
                            }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 200)
                    }
                }
                if model.options.provider != .none {
                    Divider()
                    SettingRow("Look at the footage", caption: "Better B-roll picks. Costs a little more.") {
                        Toggle("Look at the footage", isOn: $model.options.vision)
                            .labelsHidden()
                            .toggleStyle(.switch)
                    }
                    if model.options.provider.usesClaudeModels && model.options.vision {
                        Divider()
                        SettingRow("Describe clips with") {
                            Picker("Describe clips with", selection: $model.options.visionModel) {
                                ForEach(ClaudeModelChoice.describers) { choice in
                                    Text(choice.title).tag(choice.id)
                                }
                            }
                            .labelsHidden()
                            .frame(maxWidth: 300)
                        }
                    }
                }
                Divider()
                SettingRow("Project name") {
                    TextField("Project name", text: $model.options.projectName, prompt: Text("Defaults to the folder name"))
                        .labelsHidden()
                        .frame(maxWidth: 300)
                }
                Divider()
                SettingRow("Open in Final Cut Pro when it's done") {
                    Toggle("Open in Final Cut Pro when it's done", isOn: $model.options.openInFinalCutWhenDone)
                        .labelsHidden()
                        .toggleStyle(.switch)
                }
            }

            Text("Sound and picture")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.secondary)
                .padding(.bottom, -10)
            StepCard {
                SettingRow("Dialogue level") {
                    Picker("Dialogue level", selection: $model.options.dialogueLevel) {
                        ForEach(DialogueLevel.allCases) { level in
                            Text(level.title).tag(level)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 300)
                }
                Divider()
                SettingRow("Format") {
                    Picker("Format", selection: $model.options.format) {
                        // A preset's own format (720p, 1080x1920@29.97...) shows as its own row.
                        ForEach(SequenceFormat.choices(including: model.options.format)) { format in
                            Text(format.title).tag(format)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 200)
                }
                Divider()
                SettingRow(
                    "Punch-ins on jump cuts",
                    caption: "A 112% zoom alternating at each jump cut no B-roll covers, so back-to-back pieces of one shot don't jump. Auto: when Claude reads the brief asking for them (without AI, when it says so)."
                ) {
                    Picker("Punch-ins", selection: $model.options.punchIn) {
                        ForEach(PunchIn.allCases) { choice in
                            Text(choice.title).tag(choice)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .frame(width: 170)
                }
                Divider()
                SettingRow("Short fades at audio edits", caption: "No clicks at the cuts.") {
                    Toggle("Short fades at audio edits", isOn: $model.options.fades)
                        .labelsHidden()
                        .toggleStyle(.switch)
                }
                Divider()
                SettingRow("Keep “um” and “uh”") {
                    Toggle("Keep um and uh", isOn: $model.options.keepFillers)
                        .labelsHidden()
                        .toggleStyle(.switch)
                }
                Divider()
                SettingRow("Also make a dialogue-only version", caption: "All dialogue, dead air removed.") {
                    Toggle("Also make a dialogue-only version", isOn: $model.options.stringout)
                        .labelsHidden()
                        .toggleStyle(.switch)
                }
            }
        }
    }
}

/// A line of Fine-tune: what it is, in a sentence if it needs one, and its control at the right.
private struct SettingRow<Control: View>: View {
    let title: String
    let caption: String?
    private let control: Control

    init(_ title: String, caption: String? = nil, @ViewBuilder control: () -> Control) {
        self.title = title
        self.caption = caption
        self.control = control()
    }

    var body: some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 14, weight: .semibold))
                if let caption {
                    Text(caption)
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 12)
            control
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }
}
