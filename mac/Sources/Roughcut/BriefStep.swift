import RoughcutKit
import SwiftUI

/// Step two: what the video is. A preset can fill the brief, length, pace and look; everything stays editable.
struct BriefStep: View {
    @Environment(AppModel.self) private var model

    private struct Length: Identifiable {
        var label: String
        var value: String
        var id: String { label }
    }

    /// Lengths worth a click; anything else is typed.
    private static let lengths = [
        Length(label: "Auto", value: ""), Length(label: "45s", value: "45s"), Length(label: "90s", value: "90s"),
        Length(label: "3m", value: "3m"), Length(label: "10m", value: "10m"),
    ]

    var body: some View {
        @Bindable var model = model
        VStack(alignment: .leading, spacing: 18) {
            StepHeader(title: "What is this video?", subtitle: "A sentence or two is enough: who it's for, the tone, what has to stay.")
            if !model.presets.isEmpty {
                presetRow
            }
            editor
            HStack(alignment: .top, spacing: 24) {
                lengthColumn
                feelColumn
            }
        }
    }

    private var presetRow: some View {
        HStack(spacing: 8) {
            Text("Start from")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.secondary)
                .padding(.trailing, 4)
            ForEach(model.presets) { preset in
                let selected = model.options.presetName == preset.name
                Button {
                    model.applyPreset(named: preset.name)
                } label: {
                    HStack(spacing: 5) {
                        if selected {
                            Image(systemName: "checkmark").font(.system(size: 11, weight: .bold))
                        }
                        Text(Preset.displayName(for: preset.name))
                    }
                }
                .buttonStyle(ChipButtonStyle(selected: selected))
                .help(preset.description ?? "Fills the brief, length, pacing, format and graphics look from this preset. Change anything after.")
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
            let none = model.options.presetName.isEmpty
            Button("None") { model.applyPreset(named: "") }
                .buttonStyle(ChipButtonStyle(selected: none))
                .accessibilityAddTraits(none ? .isSelected : [])
            Spacer(minLength: 0)
        }
    }

    private var editor: some View {
        @Bindable var model = model
        return TextEditor(text: $model.options.brief)
            .font(.system(size: 14))
            .scrollContentBackground(.hidden)
            .padding(8)
            .frame(height: 124)
            .background(LineTheme.card, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(LineTheme.hairline, lineWidth: 1))
            .overlay(alignment: .topLeading) {
                if model.options.brief.isEmpty {
                    Text("What is this video? Who's it for, what tone, which moments have to stay…")
                        .font(.system(size: 14))
                        .foregroundStyle(.tertiary)
                        .padding(.top, 16)
                        .padding(.leading, 14)
                        .allowsHitTesting(false)
                }
            }
    }

    private var lengthColumn: some View {
        @Bindable var model = model
        let typed = model.options.targetLength.trimmingCharacters(in: .whitespacesAndNewlines)
        return VStack(alignment: .leading, spacing: 8) {
            Text("How long?")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.secondary)
            HStack(spacing: 6) {
                ForEach(Self.lengths) { choice in
                    Button(choice.label) { model.options.targetLength = choice.value }
                        .buttonStyle(ChipButtonStyle(selected: typed == choice.value))
                        .accessibilityAddTraits(typed == choice.value ? .isSelected : [])
                }
            }
            TextField("Or type a length", text: $model.options.targetLength, prompt: Text("8m, 90s, 8:30"))
                .textFieldStyle(.roundedBorder)
                .frame(maxWidth: 170)
            if !typed.isEmpty && TimeText.seconds(from: typed) == nil {
                Label("Length \(TimeText.shouldLookLike).", systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var feelColumn: some View {
        @Bindable var model = model
        return VStack(alignment: .leading, spacing: 8) {
            Text("Feel")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.secondary)
            Picker("Pacing", selection: $model.options.style) {
                ForEach(CutStyle.allCases) { style in
                    Text(style.title).tag(style)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            Text(model.options.style.detail)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
