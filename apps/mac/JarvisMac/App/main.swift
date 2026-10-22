import AppKit

let application = NSApplication.shared
let applicationDelegate = MainActor.assumeIsolated { AppDelegate() }
application.delegate = applicationDelegate
application.setActivationPolicy(.accessory)
application.run()
