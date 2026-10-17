import RoughcutKit
import SwiftUI

/// One station on the strip, for either line: the steps of a build, or the machines of a run.
struct StripItem: Identifiable, Equatable {
    var id: String
    var label: String
    var symbol: String
    var state: StationState
    var summary: String?
    /// The Add station is drawn dashed until it has been reached.
    var dashed = false
    /// The machine at work: its belt moves.
    var live = false
}

/// The assembly line along the top of the window: a belt running through a square station for each step, amber behind the
/// ones that are done. With `onSelect` the stations are buttons (the build's steps); without it the strip is a picture (a run).
struct LineStrip: View {
    let items: [StripItem]
    var onSelect: ((String) -> Void)?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                StationCell(
                    item: item,
                    isFirst: index == 0,
                    isLast: index == items.count - 1,
                    moving: item.live && !reduceMotion,
                    action: onSelect.map { select in { select(item.id) } }
                )
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .padding(.bottom, 8)
        .frame(maxWidth: .infinity)
        .background(LineTheme.strip)
    }
}

private struct StationCell: View {
    let item: StripItem
    let isFirst: Bool
    let isLast: Bool
    let moving: Bool
    var action: (() -> Void)?

    var body: some View {
        ZStack(alignment: .top) {
            HStack(spacing: 0) {
                BeltSegment(lit: item.state != .upcoming, moving: moving)
                    .opacity(isFirst ? 0 : 1)
                BeltSegment(lit: item.state == .done, moving: false)
                    .opacity(isLast ? 0 : 1)
            }
            .padding(.top, 18)
            station
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private var station: some View {
        if let action {
            Button(action: action) {
                stationLabel
            }
            .buttonStyle(.plain)
            .accessibilityLabel(accessibilityText)
        } else {
            stationLabel
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(accessibilityText)
        }
    }

    private var stationLabel: some View {
        VStack(spacing: 0) {
            StationNode(item: item)
            Text(item.label)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(item.state == .upcoming ? Color.secondary : Color.primary)
                .padding(.top, 10)
            Text(item.summary ?? " ")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.tail)
                .padding(.horizontal, 4)
        }
        .contentShape(Rectangle())
    }

    private var accessibilityText: String {
        let state: String
        switch item.state {
        case .done: state = "done"
        case .current: state = "current step"
        case .upcoming: state = "not reached yet"
        }
        return [item.label, item.summary, state].compactMap { $0 }.joined(separator: ", ")
    }
}

private struct StationNode: View {
    let item: StripItem
    private let size: CGFloat = 42

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 12)
                .fill(fill)
                .frame(width: size, height: size)
            outline
            Image(systemName: item.symbol)
                .font(.system(size: 18))
                .foregroundStyle(iconColor)
        }
        .frame(width: size, height: size)
        .background {
            if item.state == .current {
                RoundedRectangle(cornerRadius: 17)
                    .fill(LineTheme.amber.opacity(0.28))
                    .padding(-5)
            }
        }
        .overlay(alignment: .topTrailing) {
            if item.state == .done {
                Image(systemName: "checkmark")
                    .font(.system(size: 10, weight: .heavy))
                    .foregroundStyle(Color.white)
                    .frame(width: 20, height: 20)
                    .background(Circle().fill(LineTheme.finished))
                    .overlay(Circle().strokeBorder(LineTheme.strip, lineWidth: 2))
                    .offset(x: 7, y: -7)
            }
        }
    }

    private var fill: Color {
        switch item.state {
        case .done: return LineTheme.amber
        case .current: return LineTheme.card
        case .upcoming: return item.dashed ? Color.clear : LineTheme.card
        }
    }

    private var iconColor: Color {
        switch item.state {
        case .done: return LineTheme.onAmber
        case .current: return Color.primary
        case .upcoming: return Color.secondary
        }
    }

    @ViewBuilder
    private var outline: some View {
        switch item.state {
        case .done:
            EmptyView()
        case .current:
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(LineTheme.amber, lineWidth: 2)
                .frame(width: size, height: size)
        case .upcoming:
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(
                    item.dashed ? Color.secondary.opacity(0.6) : LineTheme.hairline,
                    style: StrokeStyle(lineWidth: 1.5, dash: item.dashed ? [4, 3] : [])
                )
                .frame(width: size, height: size)
        }
    }
}

/// A stretch of belt: grooved, amber once the line has reached it. While `moving` the grooves slide along.
private struct BeltSegment: View {
    let lit: Bool
    var moving = false

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: !moving)) { timeline in
            Canvas { context, size in
                let base = lit ? LineTheme.amber : Color.secondary.opacity(0.28)
                let groove = lit ? LineTheme.amberDeep : Color.secondary.opacity(0.44)
                context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(base))
                var phase: CGFloat = 0
                if moving {
                    let seconds = timeline.date.timeIntervalSinceReferenceDate
                    phase = CGFloat(seconds.truncatingRemainder(dividingBy: 0.7) / 0.7) * 10
                }
                var x = phase - 2
                while x < size.width {
                    context.fill(Path(CGRect(x: x, y: 0, width: 2, height: size.height)), with: .color(groove))
                    x += 10
                }
            }
        }
        .frame(maxWidth: .infinity)
        .frame(height: 6)
    }
}
