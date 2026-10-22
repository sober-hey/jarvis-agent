import Foundation

var failures = 0
var count = 0
let fixture = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1]
                  : "Tests/Fixtures/wisdm_1600_clapping_excerpt.txt")
do {
    try WatchCoreSuite.run(fixture: fixture) { name, passed in
        count += 1
        if !passed { failures += 1; print("FAIL: \(name)") }
        else { print("PASS: \(name)") }
    }
} catch { failures += 1; print("ERROR: \(error)") }
print("RESULT total=\(count) passed=\(count-failures) failed=\(failures)")
exit(failures == 0 ? 0 : 1)
