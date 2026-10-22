#!/usr/bin/env ruby
# Regenerate only Jarvis.xcodeproj, not parallel workers' files.
require 'xcodeproj'
require 'fileutils'
ROOT = File.expand_path('..', __dir__)
path = File.join(ROOT, 'Jarvis.xcodeproj')
project = Xcodeproj::Project.new(path)
project.root_object.compatibility_version = 'Xcode 14.0'
project.root_object.attributes['BuildIndependentTargetsInParallel'] = 'YES'
project.root_object.attributes['LastUpgradeCheck'] = '1500'
project.root_object.development_region = 'zh_CN'
project.root_object.known_regions = ['zh_CN', 'en', 'Base']
project.build_configurations.each do |config|
  config.build_settings.merge!({
    'MACOSX_DEPLOYMENT_TARGET' => '13.0', 'SDKROOT' => 'macosx',
    'SWIFT_VERSION' => '5.0', 'CLANG_ENABLE_MODULES' => 'YES',
    'ENABLE_USER_SCRIPT_SANDBOXING' => 'YES'
  })
end
sources = project.main_group.new_group('Sources', 'Sources')
config_group = project.main_group.new_group('Configuration', 'Configuration')
%w[Info.plist Jarvis.entitlements].each { |f| config_group.new_file(f) }
project.main_group.new_file('Package.swift')

modules = %w[JarvisCore JarvisAudio JarvisSpeech JarvisTransport JarvisUI]
targets = {}
modules.each do |name|
  target = project.new_target(:framework, name, :osx, '13.0', nil, :swift)
  targets[name] = target
  group = sources.new_group(name, name)
  Dir[File.join(ROOT, 'Sources', name, '*.swift')].sort.each { |f| target.add_file_references([group.new_file(File.basename(f))]) }
  target.build_configurations.each do |config|
    config.build_settings.merge!({
      'PRODUCT_BUNDLE_IDENTIFIER' => "com.jarvis.macos.#{name}",
      'DEFINES_MODULE' => 'YES', 'GENERATE_INFOPLIST_FILE' => 'YES',
      'SKIP_INSTALL' => 'YES', 'SWIFT_VERSION' => '5.0',
      'CODE_SIGN_IDENTITY' => '-', 'CODE_SIGN_STYLE' => 'Manual',
      'ENABLE_HARDENED_RUNTIME' => (config.name == 'Release' ? 'YES' : 'NO'),
      'LD_RUNPATH_SEARCH_PATHS' => ['$(inherited)', '@loader_path/../../..'],
      'SWIFT_EMIT_LOC_STRINGS' => 'NO', 'ENABLE_TESTABILITY' => 'YES'
    })
  end
end
%w[JarvisAudio JarvisSpeech JarvisTransport JarvisUI].each do |name|
  targets[name].add_dependency(targets['JarvisCore'])
  targets[name].frameworks_build_phase.add_file_reference(targets['JarvisCore'].product_reference)
end
%w[AVFoundation Accelerate AudioToolbox].each { |f| targets['JarvisAudio'].add_system_framework(f) }
%w[AppKit AVFoundation ApplicationServices].each { |f| targets['JarvisUI'].add_system_framework(f) }
%w[Security Network].each { |f| targets['JarvisTransport'].add_system_framework(f) }

remote = project.new(Xcodeproj::Project::Object::XCRemoteSwiftPackageReference)
remote.repositoryURL = 'https://github.com/daltoniam/Starscream.git'
remote.requirement = { 'kind' => 'exactVersion', 'version' => '4.0.8' }
project.root_object.package_references << remote
starscream = project.new(Xcodeproj::Project::Object::XCSwiftPackageProductDependency)
starscream.package = remote
starscream.product_name = 'Starscream'
targets['JarvisTransport'].package_product_dependencies << starscream
starscream_file = project.new(Xcodeproj::Project::Object::PBXBuildFile)
starscream_file.product_ref = starscream
targets['JarvisTransport'].frameworks_build_phase.files << starscream_file

app = project.new_target(:application, 'Jarvis', :osx, '13.0', nil, :swift)
app_group = sources.new_group('JarvisApp', 'JarvisApp')
Dir[File.join(ROOT, 'Sources', 'JarvisApp', '*.swift')].sort.each { |f| app.add_file_references([app_group.new_file(File.basename(f))]) }
app.add_system_framework('AppKit')
embed = app.new_copy_files_build_phase('Embed Frameworks')
embed.dst_subfolder_spec = '10'
modules.each do |name|
  app.add_dependency(targets[name])
  app.frameworks_build_phase.add_file_reference(targets[name].product_reference)
  file = embed.add_file_reference(targets[name].product_reference)
  file.settings = { 'ATTRIBUTES' => ['CodeSignOnCopy', 'RemoveHeadersOnCopy'] }
end
app.build_configurations.each do |config|
  config.build_settings.merge!({
    'PRODUCT_NAME' => 'Jarvis', 'PRODUCT_BUNDLE_IDENTIFIER' => 'com.jarvis.macos',
    'INFOPLIST_FILE' => 'Configuration/Info.plist', 'GENERATE_INFOPLIST_FILE' => 'NO',
    'CODE_SIGN_ENTITLEMENTS' => 'Configuration/Jarvis.entitlements',
    'CODE_SIGN_IDENTITY' => '-', 'CODE_SIGN_STYLE' => 'Manual',
    'ENABLE_APP_SANDBOX' => 'NO', 'ENABLE_HARDENED_RUNTIME' => (config.name == 'Release' ? 'YES' : 'NO'),
    'SWIFT_VERSION' => '5.0', 'SWIFT_EMIT_LOC_STRINGS' => 'NO',
    'LD_RUNPATH_SEARCH_PATHS' => ['$(inherited)', '@executable_path/../Frameworks'],
    'MARKETING_VERSION' => '1.0.0', 'CURRENT_PROJECT_VERSION' => '1',
    'ENABLE_TESTABILITY' => 'YES'
  })
end

test = project.new_target(:unit_test_bundle, 'JarvisCoreTests', :osx, '13.0', nil, :swift)
test_group = project.main_group.new_group('Tests', 'Tests').new_group('JarvisCoreTests', 'JarvisCoreTests')
Dir[File.join(ROOT, 'Tests', 'JarvisCoreTests', '*.swift')].sort.each { |f| test.add_file_references([test_group.new_file(File.basename(f))]) }
test.add_dependency(targets['JarvisCore'])
test.frameworks_build_phase.add_file_reference(targets['JarvisCore'].product_reference)
test.build_configurations.each do |config|
  config.build_settings.merge!({
    'PRODUCT_BUNDLE_IDENTIFIER' => 'com.jarvis.macos.tests', 'GENERATE_INFOPLIST_FILE' => 'YES',
    'SWIFT_VERSION' => '5.0', 'CODE_SIGN_IDENTITY' => '-', 'CODE_SIGN_STYLE' => 'Manual',
    'LD_RUNPATH_SEARCH_PATHS' => ['$(inherited)', '@loader_path/../Frameworks', '@loader_path/../../../'],
    'TEST_HOST' => '', 'BUNDLE_LOADER' => ''
  })
end
project.save
scheme = Xcodeproj::XCScheme.new
scheme.add_build_target(app)
scheme.set_launch_target(app)
scheme.add_test_target(test)
scheme.save_as(path, 'Jarvis', true)
targets.each do |name, target|
  s = Xcodeproj::XCScheme.new
  s.add_build_target(target)
  s.save_as(path, name, true)
end
workspace_dir = File.join(path, 'project.xcworkspace')
FileUtils.mkdir_p(workspace_dir)
File.write(File.join(workspace_dir, 'contents.xcworkspacedata'), %(<?xml version="1.0" encoding="UTF-8"?><Workspace version="1.0"><FileRef location="self:"></FileRef></Workspace>))
puts "Generated #{path}: five framework targets + app + unit tests; Starscream via SPM"
