import Foundation

/// How far one pipeline stage of a run has got (the run screen reads it, and `MachineLine` rolls it up).
public enum StageStatus: Equatable, Sendable {
    case pending, active, done, skipped
}

/// The machines the line runs while a cut is built: the pipeline's nine stages as the six steps a person thinks in.
public enum Machine: String, CaseIterable, Identifiable, Sendable {
    case listen, watch, plan, cut, graphics, export

    public var id: String { rawValue }

    /// The pipeline stages this machine does, in order.
    public var stages: [Stage] {
        switch self {
        case .listen: return [.scan, .probe, .silence, .transcribe]
        case .watch: return [.vision]
        case .plan: return [.plan]
        case .cut: return [.cut]
        case .graphics: return [.graphics]
        case .export: return [.write]
        }
    }

    public static func of(_ stage: Stage) -> Machine {
        switch stage {
        case .scan, .probe, .silence, .transcribe: return .listen
        case .vision: return .watch
        case .plan: return .plan
        case .cut: return .cut
        case .graphics: return .graphics
        case .write: return .export
        }
    }

    /// Under its node.
    public var label: String {
        switch self {
        case .listen: return "Listen"
        case .watch: return "Watch"
        case .plan: return "Plan"
        case .cut: return "Cut"
        case .graphics: return "Graphics"
        case .export: return "Export"
        }
    }

    /// An SF Symbol.
    public var symbol: String {
        switch self {
        case .listen: return "waveform"
        case .watch: return "eye"
        case .plan: return "checklist"
        case .cut: return "scissors"
        case .graphics: return "wand.and.stars"
        case .export: return "square.and.arrow.down"
        }
    }

    /// The heading while it works.
    public var heading: String {
        switch self {
        case .listen: return "Listening to your footage"
        case .watch: return "Looking at the footage"
        case .plan: return "Planning the edit"
        case .cut: return "Cutting"
        case .graphics: return "Making the graphics"
        case .export: return "Writing the Final Cut file"
        }
    }

    /// What it is doing, in a sentence.
    public var explanation: String {
        switch self {
        case .listen: return "Roughcut reads every clip, finds the pauses and writes down what people say."
        case .watch: return "Claude looks at frames from your clips so the B-roll lands on the right shots."
        case .plan: return "Claude reads every transcript and clip note, then chooses which lines to keep, in what order, and where the B-roll goes."
        case .cut: return "Roughcut turns the plan into frame-accurate cuts, evens out the sound and places the B-roll."
        case .graphics: return "Claude designs each graphic, then Roughcut draws it over the footage."
        case .export: return "Roughcut writes the Final Cut file, the captions and the notes on what it did."
        }
    }

    /// Once it has finished.
    public var doneLine: String {
        switch self {
        case .listen: return "Listened to your footage and found the pauses"
        case .watch: return "Looked at the footage"
        case .plan: return "Planned the edit"
        case .cut: return "Cut the edit"
        case .graphics: return "Made the graphics"
        case .export: return "Wrote the Final Cut file"
        }
    }
}

/// A machine on the strip while a run goes: where it has got to.
public struct MachineNode: Equatable, Identifiable, Sendable {
    public var machine: Machine
    public var status: StageStatus

    public var id: String { machine.rawValue }
}

public enum MachineLine {
    /// The machines a run of this kind goes through. `options` (a build's form) leaves out what it won't run: Watch without
    /// a model that looks at frames, Graphics without graphics. A re-cut doesn't know, so it lists Graphics and the strip
    /// drops it once the pipeline has gone past it without a graphics stage.
    public static func machines(for kind: RunKind, options: BuildOptions?) -> [Machine] {
        switch kind {
        case .build:
            var list: [Machine] = [.listen]
            if options.map({ $0.provider.usesAI && $0.vision }) ?? true { list.append(.watch) }
            list += [.plan, .cut]
            if options.map(\.graphicsActive) ?? true { list.append(.graphics) }
            list.append(.export)
            return list
        case .recut, .finishGraphics:
            return [.cut, .graphics, .export]
        case .revise:
            return [.plan, .cut, .graphics, .export]
        }
    }

    /// A machine's status from its stages': working while any of them is; skipped when all were; done when each finished
    /// or was skipped; working too when some finished and the next hasn't been reported yet.
    public static func status(of machine: Machine, in stages: [Stage: StageStatus]) -> StageStatus {
        let list = machine.stages.map { stages[$0] ?? .pending }
        if list.contains(.active) { return .active }
        if list.allSatisfy({ $0 == .skipped }) { return .skipped }
        if list.allSatisfy({ $0 == .done || $0 == .skipped }) { return .done }
        if list.contains(.done) { return .active }
        return .pending
    }

    /// The strip: each machine of the run with its status, the ones the pipeline passed without running left out.
    public static func nodes(for kind: RunKind, options: BuildOptions?, stages: [Stage: StageStatus]) -> [MachineNode] {
        machines(for: kind, options: options)
            .map { MachineNode(machine: $0, status: status(of: $0, in: stages)) }
            .filter { $0.status != .skipped }
    }

    /// The machine at work (or, before the first stage is reported, the first), with its place in the line: "Step 3 of 6".
    public static func current(in nodes: [MachineNode]) -> (machine: Machine, number: Int, of: Int)? {
        let index = nodes.firstIndex { $0.status == .active } ?? nodes.firstIndex { $0.status == .pending }
        guard let index else { return nil }
        return (nodes[index].machine, index + 1, nodes.count)
    }
}
