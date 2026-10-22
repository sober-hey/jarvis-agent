#!/usr/bin/env ruby
# Generates only this module's independent verification project.
require 'xcodeproj'
root = File.expand_path(__dir__)
path = File.join(root, 'AudioAccessibility.xcodeproj')
project = Xcodeproj::Project.new(path)
target = project.new_target(:unit_test_bundle, 'AudioAccessibilityTests', :osx, '14.0', nil, :swift)
group = project.main_group.new_group('Module')
%w[AudioPlayer.swift AccessibilityHelper.swift PlaybackMenuPresenter.swift Tests/ModuleXcodeTests.swift].each do |name|
  target.add_file_references([group.new_file(name)])
end
resources = project.main_group.new_group('Fixtures', 'Tests/Fixtures')
target.resources_build_phase.add_file_reference(resources.new_file('fixture.mp3'))
%w[Foundation AppKit AVFoundation Combine ApplicationServices Carbon XCTest].each { |name| target.add_system_framework(name) }
project.build_configurations.each do |config|
  config.build_settings['SDKROOT'] = 'macosx'
  config.build_settings['SWIFT_VERSION'] = '6.0'
  config.build_settings['MACOSX_DEPLOYMENT_TARGET'] = '14.0'
end
target.build_configurations.each do |config|
  config.build_settings.merge!({
    'SWIFT_VERSION' => '6.0', 'SWIFT_TREAT_WARNINGS_AS_ERRORS' => 'YES',
    'PRODUCT_BUNDLE_IDENTIFIER' => 'com.jarvis.audioaccessibility.tests',
    'GENERATE_INFOPLIST_FILE' => 'YES', 'CODE_SIGNING_ALLOWED' => 'NO',
    'ENABLE_APP_SANDBOX' => 'NO', 'TEST_HOST' => '', 'BUNDLE_LOADER' => '',
    'LD_RUNPATH_SEARCH_PATHS' => ['$(inherited)', '@loader_path/../Frameworks', '@executable_path/../Frameworks'],
    'SWIFT_EMIT_LOC_STRINGS' => 'NO'
  })
end
project.save
scheme = Xcodeproj::XCScheme.new
scheme.add_build_target(target)
scheme.add_test_target(target)
scheme.test_action.build_configuration = 'Debug'
scheme.save_as(path, 'AudioAccessibility', true)
puts path
