import Foundation

public extension RunResult {
    /// Who planned the cut, in words: `planned_by` ("claude-code:claude-opus-5-5") as "Claude Opus 5.5, through your Claude plan".
    /// A cut made without a model says what the engine said.
    var plannerSentence: String {
        guard let colon = plannedBy.firstIndex(of: ":") else {
            return plannedBy.isEmpty ? "no AI" : plannedBy
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
