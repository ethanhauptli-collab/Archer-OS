import AppKit
import SwiftUI

/// The look of the step-by-step flow: the system's own colors with one amber accent (the one in the app icon), so light and
/// dark both work and the controls stay native.
enum LineTheme {
    static let amber = Color(red: 0.961, green: 0.647, blue: 0.141)
    static let amberDeep = Color(red: 0.890, green: 0.584, blue: 0.075)
    /// Text and icons on amber.
    static let onAmber = Color(red: 0.122, green: 0.082, blue: 0.0)
    static let finished = Color(red: 0.118, green: 0.482, blue: 0.227)

    static var card: Color { Color(nsColor: .controlBackgroundColor) }
    static var page: Color { Color(nsColor: .windowBackgroundColor) }
    static var strip: Color { Color(nsColor: .controlBackgroundColor) }
    static var hairline: Color { Color(nsColor: .separatorColor) }
}

/// The main button of a step: amber, with dark text.
struct LinePrimaryButtonStyle: ButtonStyle {
    var large = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: large ? 16 : 15, weight: .semibold))
            .foregroundStyle(isEnabled ? LineTheme.onAmber : Color.secondary)
            .padding(.horizontal, large ? 24 : 20)
            .frame(height: large ? 48 : 44)
            .background(
                RoundedRectangle(cornerRadius: large ? 12 : 11)
                    .fill(isEnabled ? LineTheme.amber : Color.secondary.opacity(0.18))
            )
            .opacity(configuration.isPressed ? 0.85 : 1)
    }
}

/// A choice among a few: a preset, a length. The chosen one is amber-edged and bold.
struct ChipButtonStyle: ButtonStyle {
    var selected: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 14, weight: selected ? .semibold : .regular))
            .foregroundStyle(Color.primary)
            .padding(.horizontal, 12)
            .frame(minWidth: 44, minHeight: 34)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(selected ? LineTheme.amber.opacity(0.18) : LineTheme.card)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(selected ? LineTheme.amber : LineTheme.hairline, lineWidth: selected ? 1.5 : 1)
            )
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

/// A step's title and the sentence under it.
struct StepHeader: View {
    let title: String
    var subtitle: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.system(size: 28, weight: .bold))
                .tracking(-0.4)
            if let subtitle {
                Text(subtitle)
                    .font(.system(size: 15))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// A rounded panel for rows; the rows add their own dividers.
struct StepCard<Content: View>: View {
    private let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LineTheme.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(LineTheme.hairline, lineWidth: 1))
    }
}

/// A square tile with a symbol: amber when it stands for something on the line.
struct IconTile: View {
    let symbol: String
    var tint = false
    var size: CGFloat = 38

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: size * 0.5))
            .foregroundStyle(tint ? LineTheme.onAmber : Color.secondary)
            .frame(width: size, height: size)
            .background(
                RoundedRectangle(cornerRadius: size * 0.28)
                    .fill(tint ? LineTheme.amber : Color.secondary.opacity(0.14))
            )
    }
}

/// A dashed outline that takes drops; amber while something is held over it.
struct DashedZone: ViewModifier {
    var targeted: Bool
    var radius: CGFloat

    func body(content: Content) -> some View {
        content.background {
            RoundedRectangle(cornerRadius: radius)
                .fill(targeted ? LineTheme.amber.opacity(0.08) : Color.secondary.opacity(0.04))
            RoundedRectangle(cornerRadius: radius)
                .strokeBorder(targeted ? LineTheme.amber : LineTheme.hairline, style: StrokeStyle(lineWidth: 2, dash: [7, 5]))
        }
    }
}

extension View {
    func dashedZone(targeted: Bool = false, radius: CGFloat = 18) -> some View {
        modifier(DashedZone(targeted: targeted, radius: radius))
    }
}

/// "Added", on a part that is on the line.
struct AddedPill: View {
    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: "checkmark").font(.system(size: 11, weight: .heavy))
            Text("Added").font(.system(size: 13, weight: .semibold))
        }
        .foregroundStyle(LineTheme.onAmber)
        .padding(.horizontal, 12)
        .frame(height: 30)
        .background(Capsule().fill(LineTheme.amber))
    }
}
