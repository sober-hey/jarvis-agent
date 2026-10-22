import Foundation
import JarvisCore

var failures = 0
var checks = 0
func check(_ condition: @autoclosure () -> Bool, _ label: String) {
    checks += 1
    if !condition() { failures += 1; print("FAIL: \(label)") }
}
let state = RuntimeState()
check(!state.isListening && state.indicator == .off, "starts off")
state.setListening(true)
check(state.isListening && state.indicator == .listening, "toggle on")
for (activity, indicator) in [(Activity.recording, Indicator.recording), (.processing, .processing), (.playing, .playing)] {
    try state.setActivity(activity)
    check(state.indicator == indicator, "activity \(activity)")
}
state.setListening(false)
check(!state.isListening && state.indicator == .off, "toggle off clears activity")
do {
    try state.setActivity(.recording)
    check(false, "activity while off is rejected")
} catch { check(true, "activity rejected") }
state.setListening(true)
state.report(.permissionDenied("microphone"))
check(state.indicator == .failed && state.lastError != nil, "error visible")
state.clearError()
check(state.indicator == .listening && state.lastError == nil, "error recovery")
print("Core smoke: \(checks) checks, \(failures) failures")
exit(failures == 0 ? 0 : 1)
