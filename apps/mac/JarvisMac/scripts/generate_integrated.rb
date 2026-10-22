#!/usr/bin/env ruby
# Owns only the formal project. App and JarvisIntegration sources belong to the composition root.
require 'xcodeproj'
require 'fileutils'
require 'json'
ROOT = File.expand_path('..', __dir__)
PROJECT_PATH = File.join(ROOT, 'JarvisMacApp.xcodeproj')
TEAM = '956UD85933'
project = Xcodeproj::Project.new(PROJECT_PATH)
project.root_object.compatibility_version = 'Xcode 14.0'
project.root_object.attributes['BuildIndependentTargetsInParallel'] = 'YES'
project.root_object.attributes['LastUpgradeCheck'] = '2710'
project.root_object.development_region = 'zh_CN'
project.root_object.known_regions = %w[zh_CN en Base]
project.build_configurations.each do |c|
  c.build_settings.merge!('SDKROOT' => 'macosx', 'MACOSX_DEPLOYMENT_TARGET' => '13.0',
    'SWIFT_VERSION' => '5.0', 'CLANG_ENABLE_MODULES' => 'YES',
    'ENABLE_USER_SCRIPT_SANDBOXING' => 'YES')
end
refs = {}
reference = lambda do |relative|
  refs[relative] ||= project.main_group.new_file(relative)
end
new_target = lambda do |type, name, sources|
  t = project.new_target(type, name, :osx, '13.0', nil, :swift)
  t.add_file_references(sources.sort.map { |s| reference.call(s) })
  t.build_configurations.each do |c|
    c.build_settings.merge!(
      'PRODUCT_BUNDLE_IDENTIFIER' => "com.jarvis.macos.#{name}",
      'PRODUCT_MODULE_NAME' => name, 'SWIFT_VERSION' => '5.0',
      'DEVELOPMENT_TEAM' => TEAM, 'CODE_SIGN_STYLE' => 'Automatic',
      'CODE_SIGN_IDENTITY' => 'Apple Development',
      'GENERATE_INFOPLIST_FILE' => 'YES', 'ENABLE_TESTABILITY' => 'YES',
      'SWIFT_EMIT_LOC_STRINGS' => 'NO', 'SKIP_INSTALL' => 'YES',
      'LD_RUNPATH_SEARCH_PATHS' => ['$(inherited)', '$(BUILT_PRODUCTS_DIR)', '@executable_path',
        '@executable_path/../Frameworks', '@loader_path/../../..'])
    c.build_settings['SWIFT_OPTIMIZATION_LEVEL'] = '-Onone' if c.name == 'Debug'
    c.build_settings['DEFINES_MODULE'] = 'YES' if type == :framework
    if type == :unit_test_bundle
      c.build_settings['TEST_HOST'] = ''
      c.build_settings['BUNDLE_LOADER'] = ''
    end
  end
  t
end
glob = lambda { |pattern| Dir[File.join(ROOT, pattern)].sort.map { |f| f.delete_prefix(ROOT + '/') } }
modules = {
  'JarvisCore' => glob.call('Sources/JarvisCore/*.swift'),
  'JarvisUI' => glob.call('Sources/JarvisUI/*.swift'),
  'AudioRecorderModule' => glob.call('Vendor/AudioRecorder/Sources/*.swift'),
  'JarvisActivation' => glob.call('Vendor/Activation/Sources/JarvisActivation/*.swift'),
  'JarvisWebSocket' => glob.call('Vendor/WebSocket/Sources/JarvisWebSocket/*.swift'),
  'AudioAccessibilityModule' => glob.call('Vendor/AudioAccessibility/*.swift'),
  'JarvisIntegration' => glob.call('Sources/JarvisIntegration/*.swift')
}
expected = {'AudioRecorderModule'=>4, 'JarvisActivation'=>6, 'JarvisWebSocket'=>2, 'AudioAccessibilityModule'=>3}
expected.each { |name, count| abort "#{name}: expected #{count} source files, got #{modules[name].length}" unless modules[name].length == count }
targets = modules.map { |name, files| [name, new_target.call(:framework, name, files)] }.to_h
link = lambda do |consumer, dependency|
  consumer.add_dependency(dependency)
  consumer.frameworks_build_phase.add_file_reference(dependency.product_reference)
end
link.call(targets['JarvisUI'], targets['JarvisCore'])
(modules.keys - ['JarvisIntegration']).each { |name| link.call(targets['JarvisIntegration'], targets[name]) }
{
  'JarvisUI' => %w[AppKit AVFoundation ApplicationServices],
  'AudioRecorderModule' => %w[AVFoundation],
  'JarvisActivation' => %w[AVFoundation Accelerate CoreML],
  'JarvisWebSocket' => %w[Combine Security],
  'AudioAccessibilityModule' => %w[AppKit AVFoundation Combine ApplicationServices Carbon],
  'JarvisIntegration' => %w[AppKit AVFoundation Combine CoreML Security]
}.each { |name, frameworks| frameworks.each { |fw| targets[name].add_system_framework(fw) } }
remote = project.new(Xcodeproj::Project::Object::XCRemoteSwiftPackageReference)
remote.repositoryURL = 'https://github.com/daltoniam/Starscream.git'
remote.requirement = {'kind'=>'exactVersion', 'version'=>'4.0.8'}
project.root_object.package_references << remote
starscream = project.new(Xcodeproj::Project::Object::XCSwiftPackageProductDependency)
starscream.package = remote
starscream.product_name = 'Starscream'
targets['JarvisWebSocket'].package_product_dependencies << starscream
package_build_file = project.new(Xcodeproj::Project::Object::PBXBuildFile)
package_build_file.product_ref = starscream
targets['JarvisWebSocket'].frameworks_build_phase.files << package_build_file

app = new_target.call(:application, 'JarvisMacApp', glob.call('App/*.swift'))
app.add_system_framework('AppKit')
app.add_system_framework('ServiceManagement')
glob.call('Resources/WakeWord/*.mlmodelc').each do |f|
  ref = reference.call(f); ref.last_known_file_type = 'wrapper.cfbundle'
  app.resources_build_phase.add_file_reference(ref)
end
glob.call('Tests/ReplayFixtures/*.wav').each { |f| app.resources_build_phase.add_file_reference(reference.call(f)) }
embed = app.new_copy_files_build_phase('Embed Frameworks')
embed.dst_subfolder_spec = '10'
targets.each_value do |t|
  link.call(app, t)
  bf = embed.add_file_reference(t.product_reference)
  bf.settings = {'ATTRIBUTES'=>%w[CodeSignOnCopy RemoveHeadersOnCopy]}
end
app.build_configurations.each do |c|
  c.build_settings.merge!(
    'PRODUCT_NAME'=>'JarvisMacApp', 'PRODUCT_BUNDLE_IDENTIFIER'=>'com.jarvis.macos',
    'INFOPLIST_FILE'=>'App/Info.plist', 'GENERATE_INFOPLIST_FILE'=>'NO',
    'CODE_SIGN_ENTITLEMENTS'=>'App/JarvisMacApp.entitlements',
    'ENABLE_APP_SANDBOX'=>'YES', 'ENABLE_HARDENED_RUNTIME'=>'YES',
    'SKIP_INSTALL'=>'NO', 'MARKETING_VERSION'=>'1.0.0', 'CURRENT_PROJECT_VERSION'=>'1')
end
%w[App/Info.plist App/JarvisMacApp.entitlements README.md].each { |f| reference.call(f) }

test_targets = []
[
  ['JarvisCoreTests', 'Tests/JarvisCoreTests/*.swift', 'JarvisCore'],
  ['JarvisIntegrationTests', 'Tests/JarvisIntegrationTests/*.swift', 'JarvisIntegration'],
  ['JarvisWebSocketTests', 'Vendor/WebSocket/Tests/JarvisWebSocketTests/*.swift', 'JarvisWebSocket'],
  ['AudioAccessibilityTests', 'Vendor/AudioAccessibility/Tests/ModuleXcodeTests.swift', 'AudioAccessibilityModule']
].each do |name, pattern, dependency|
  t = new_target.call(:unit_test_bundle, name, glob.call(pattern))
  link.call(t, targets[dependency])
  t.add_system_framework('XCTest')
  if name == 'JarvisIntegrationTests'
    (targets.values - [targets[dependency]]).each { |dep| link.call(t, dep) }
    %w[AVFoundation Security].each { |fw| t.add_system_framework(fw) }
  end
  if name == 'AudioAccessibilityTests'
    t.resources_build_phase.add_file_reference(reference.call('Vendor/AudioAccessibility/Tests/Fixtures/fixture.mp3'))
    t.add_system_framework('AppKit')
    t.add_system_framework('ApplicationServices')
  end
  test_targets << t
end

commands = {}
commands['AudioRecorderTests'] = new_target.call(:command_line_tool, 'AudioRecorderTests', modules['AudioRecorderModule'] + ['Vendor/AudioRecorder/Tests/main.swift'])
commands['AudioRecorderTests'].add_system_framework('AVFoundation')
%w[ActivationRegression ActivationReplay].each do |name|
  commands[name] = new_target.call(:command_line_tool, name, ["Vendor/Activation/Tests/#{name}/main.swift"])
  link.call(commands[name], targets['JarvisActivation'])
end
# These standalone checks intentionally compile the source alongside the original tests,
# retaining access to internal injected backends without modifying the test body.
%w[AudioPlayerChecks AccessibilityChecks].each do |name|
  t = new_target.call(:command_line_tool, name, modules['AudioAccessibilityModule'] + ["Vendor/AudioAccessibility/Tests/#{name}.swift"])
  t.build_configurations.each { |c| c.build_settings['OTHER_SWIFT_FLAGS'] = ['$(inherited)', '-parse-as-library'] }
  %w[AppKit AVFoundation Combine ApplicationServices Carbon].each { |fw| t.add_system_framework(fw) }
  commands[name] = t
end
# Original optional real-microphone / loopback harnesses are buildable, never auto-run.
recorder_cli = new_target.call(:command_line_tool, 'AudioRecorderCLI', modules['AudioRecorderModule'] + ['Vendor/AudioRecorder/CLI/main.swift'])
recorder_cli.add_system_framework('AVFoundation')
recorder_cli.build_configurations.each { |c| c.build_settings['INFOPLIST_KEY_NSMicrophoneUsageDescription'] = 'Record audio only when this explicit microphone harness is launched.' }
loopback = new_target.call(:command_line_tool, 'WebSocketLoopbackClient', ['Tests/WebSocketLoopback/main.swift'])
link.call(loopback, targets['JarvisWebSocket'])
optional_commands = {'AudioRecorderCLI'=>recorder_cli, 'WebSocketLoopbackClient'=>loopback}

project.save
# Delete only generated shared schemes in this owned formal project.
schemes_path = File.join(PROJECT_PATH, 'xcshareddata', 'xcschemes')
FileUtils.rm_rf(schemes_path)
scheme_for = lambda do |name, build_targets, tests, launch|
  s = Xcodeproj::XCScheme.new
  build_targets.each { |t| s.add_build_target(t) }
  tests.each { |t| s.add_test_target(t) }
  s.set_launch_target(launch) if launch
  s.save_as(PROJECT_PATH, name, true)
end
scheme_for.call('JarvisMacApp', [app], test_targets, app)
targets.each { |name, t| scheme_for.call(name, [t], [], nil) }
test_targets.each { |t| scheme_for.call(t.name, [t], [t], nil) }
# Permission prompt stays in the original test source but is opt-in, not run by default.
# The copied tests can be invoked explicitly with -only-testing if user-authorized.
%w[JarvisMacApp AudioAccessibilityTests ModuleTests].each do |scheme_name|
  next if scheme_name == 'ModuleTests'
  s = Xcodeproj::XCScheme.new(File.join(schemes_path, "#{scheme_name}.xcscheme"))
  s.test_action.testables.each do |testable|
    next unless testable.xml_element.to_s.include?('AudioAccessibilityTests')
    skipped = testable.xml_element.elements['SkippedTests'] || testable.xml_element.add_element('SkippedTests')
    skipped.add_element('Test', {'Identifier'=>'ModuleXcodeTests/testExplicitSystemPermissionPrompt()'})
  end
  s.save_as(PROJECT_PATH, scheme_name, true)
end
(commands.merge(optional_commands)).each { |name, t| scheme_for.call(name, [t], [], t) }
# Build/test this scheme without compiling the parent-owned app/integration composition root.
scheme_for.call('ModuleTests', [*test_targets, *commands.values], test_targets, nil)
scheme_for.call('AllModuleTests', [*test_targets, *commands.values], test_targets, nil)
s = Xcodeproj::XCScheme.new(File.join(schemes_path, 'ModuleTests.xcscheme'))
s.test_action.testables.each do |testable|
  next unless testable.xml_element.to_s.include?('AudioAccessibilityTests')
  skipped = testable.xml_element.elements['SkippedTests'] || testable.xml_element.add_element('SkippedTests')
  skipped.add_element('Test', {'Identifier'=>'ModuleXcodeTests/testExplicitSystemPermissionPrompt()'})
end
s.save_as(PROJECT_PATH, 'ModuleTests', true)
workspace = File.join(PROJECT_PATH, 'project.xcworkspace')
FileUtils.mkdir_p(workspace)
File.write(File.join(workspace, 'contents.xcworkspacedata'), '<?xml version="1.0" encoding="UTF-8"?><Workspace version="1.0"><FileRef location="self:"/></Workspace>')
resolved_dir = File.join(workspace, 'xcshareddata', 'swiftpm')
FileUtils.mkdir_p(resolved_dir)
FileUtils.cp(File.join(ROOT, 'Package.resolved'), File.join(resolved_dir, 'Package.resolved'))
puts JSON.pretty_generate({project: PROJECT_PATH, app: app.name, team: TEAM,
  modules: modules, test_targets: test_targets.map(&:name), replay_executables: commands.keys,
  optional_harnesses: optional_commands.keys,
  explicit_permission_test: 'not run by default; skip entry preserved', starscream: '4.0.8'})
