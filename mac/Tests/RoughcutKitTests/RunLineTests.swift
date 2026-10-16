import XCTest
@testable import RoughcutKit

/// The nine pipeline stages as the six machines the run screen shows.
final class RunLineTests: XCTestCase {
    private func options(graphics: Bool = false, vision: Bool = true) -> BuildOptions {
        var options = BuildOptions()
        options.choose(provider: .claudeCode)
        options.vision = vision
        options.setGraphics(graphics)
        return options
    }

    func testEveryStageBelongsToExactlyOneMachineInOrder() {
        XCTAssertEqual(Machine.allCases.flatMap(\.stages), Stage.allCases)
        for stage in Stage.allCases {
            XCTAssertTrue(Machine.of(stage).stages.contains(stage), "\(stage)")
        }
    }

    func testTheMachinesOfEachKindOfRun() {
        XCTAssertEqual(MachineLine.machines(for: .build, options: options()), [.listen, .watch, .plan, .cut, .export])
        XCTAssertEqual(MachineLine.machines(for: .build, options: options(graphics: true)), [.listen, .watch, .plan, .cut, .graphics, .export])
        XCTAssertEqual(MachineLine.machines(for: .build, options: options(graphics: true, vision: false)), [.listen, .plan, .cut, .graphics, .export])

        var none = options(graphics: true)
        none.choose(provider: .none)
        XCTAssertEqual(MachineLine.machines(for: .build, options: none), [.listen, .plan, .cut, .export], "no model: nothing looks or designs")

        XCTAssertEqual(MachineLine.machines(for: .build, options: nil), [.listen, .watch, .plan, .cut, .graphics, .export])
        XCTAssertEqual(MachineLine.machines(for: .recut, options: nil), [.cut, .graphics, .export])
        XCTAssertEqual(MachineLine.machines(for: .finishGraphics, options: nil), [.cut, .graphics, .export])
        XCTAssertEqual(MachineLine.machines(for: .revise, options: nil), [.plan, .cut, .graphics, .export])
    }

    func testAMachineIsWorkingWhileAnyOfItsStagesIs() {
        XCTAssertEqual(MachineLine.status(of: .listen, in: [:]), .pending)
        XCTAssertEqual(MachineLine.status(of: .listen, in: [.scan: .done, .probe: .active]), .active)
        XCTAssertEqual(MachineLine.status(of: .cut, in: [.cut: .active]), .active)
        XCTAssertEqual(MachineLine.status(of: .listen, in: [.scan: .done]), .active, "between two of its own stages")
    }

    func testAMachineIsDoneWhenEachStageFinishedOrWasSkipped() {
        let finished: [Stage: StageStatus] = [.scan: .done, .probe: .done, .silence: .done, .transcribe: .done]
        XCTAssertEqual(MachineLine.status(of: .listen, in: finished), .done)
        var transcriptFound = finished
        transcriptFound[.transcribe] = .skipped  // a sidecar transcript needed no engine
        XCTAssertEqual(MachineLine.status(of: .listen, in: transcriptFound), .done)
    }

    func testAMachineIsSkippedOnlyWhenAllItsStagesWere() {
        XCTAssertEqual(MachineLine.status(of: .watch, in: [.vision: .skipped]), .skipped)
        XCTAssertEqual(MachineLine.status(of: .listen, in: [.scan: .skipped, .probe: .skipped, .silence: .skipped, .transcribe: .skipped]), .skipped)
    }

    func testTheStripDropsMachinesThePipelinePassedWithoutRunning() {
        // A re-cut of a cut with no graphics: the pipeline went from cut to write.
        let stages: [Stage: StageStatus] = [.cut: .done, .graphics: .skipped, .write: .active]
        let nodes = MachineLine.nodes(for: .recut, options: nil, stages: stages)
        XCTAssertEqual(nodes.map(\.machine), [.cut, .export])
        XCTAssertEqual(nodes.map(\.status), [.done, .active])
    }

    func testCurrentIsTheMachineAtWorkOrTheFirstToCome() {
        var nodes = MachineLine.nodes(for: .build, options: nil, stages: [:])
        let first = MachineLine.current(in: nodes)
        XCTAssertEqual(first?.machine, .listen)
        XCTAssertEqual(first?.number, 1)
        XCTAssertEqual(first?.of, 6)

        let listened: [Stage: StageStatus] = [.scan: .done, .probe: .done, .silence: .done, .transcribe: .done, .vision: .done, .plan: .active]
        nodes = MachineLine.nodes(for: .build, options: nil, stages: listened)
        let planning = MachineLine.current(in: nodes)
        XCTAssertEqual(planning?.machine, .plan)
        XCTAssertEqual(planning?.number, 3)

        var allDone: [Stage: StageStatus] = [:]
        for stage in Stage.allCases {
            allDone[stage] = .done
        }
        XCTAssertNil(MachineLine.current(in: MachineLine.nodes(for: .build, options: nil, stages: allDone)), "nothing is left to run")
    }

    func testEachMachineHasWordsForEveryMoment() {
        for machine in Machine.allCases {
            XCTAssertFalse(machine.label.isEmpty)
            XCTAssertFalse(machine.heading.isEmpty)
            XCTAssertFalse(machine.explanation.isEmpty)
            XCTAssertFalse(machine.doneLine.isEmpty)
            XCTAssertFalse(machine.symbol.isEmpty)
        }
    }
}
