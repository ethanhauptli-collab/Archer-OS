import Foundation

public extension RunResult {
    /// The big line over a finished cut: the cut is ready, unless it was written before its graphics were made (or some of the
    /// ones it has room for weren't designed or didn't render, as after a usage limit), or the AI step failed.
    var headline: String {
        if fellBack { return "Made without the AI" }
        if partial == true { return "Written, but the graphics aren't made yet" }
        if let graphics, graphics.planned > 0, graphics.rendered < graphics.placed {
            return graphics.rendered == 0 ? "Written, but the graphics aren't made yet" : "Written, but some graphics aren't made yet"
        }
        return "Your rough cut is ready"
    }

    /// The line under the headline of a finished cut: its version, and who planned it (or that nobody did).
    var subtitle: String {
        if fellBack {
            return "The AI step failed, so this is the stringout with dead air removed. The warnings say why."
        }
        let number = version.map { "Version \($0). " } ?? ""
        let unfinished = partial == true ? "No graphics yet. " : ""
        let planner = madeWithoutAI ? "Made without AI: the dead air is cut and nothing is reordered." : "Planned by \(plannerSentence)."
        return number + unfinished + planner
    }

    /// Who planned the cut, in words: `planned_by` ("claude-code:claude-opus-5-5") as "Claude Opus 5.5, through your Claude plan".
    /// A cut made without a model says what the engine said.
    var plannerSentence: String {
        guard let colon = plannedBy.firstIndex(of: ":") else {
            return plannedBy.isEmpty || plannedBy == "heuristic" ? "no AI" : plannedBy
        }
        let provider = String(plannedBy[..<colon])
        let model = String(plannedBy[plannedBy.index(after: colon)...])
        let name = Self.modelName(model)
        switch provider {
        case "claude-code": return "\(name), through your Claude plan"
        case "anthropic": return "\(name), through your API key"
        case "openai": return "\(name) (OpenAI)"
        case "ollama": return "\(name) (on this Mac)"
        default: return "\(name) (\(provider))"
        }
    }

    /// "claude-opus-5-5" as "Claude Opus 5.5"; any other model name as it is.
    internal static func modelName(_ model: String) -> String {
        let parts = model.split(separator: "-").map(String.init)
        let families = ["opus", "sonnet", "haiku", "fable"]
        if parts.count >= 4, parts[0] == "claude", families.contains(parts[1]), let major = Int(parts[2]), let minor = Int(parts[3]) {
            return "Claude \(parts[1].capitalized) \(major).\(minor)"
        }
        return model
    }
}
