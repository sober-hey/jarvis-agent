#!/usr/bin/env python3
# ruff: noqa: E501 -- generated pbxproj/XML string literals remain single-line.
"""Regenerate the dependency-free Xcode project; no xcodegen/gems required."""

from __future__ import annotations

import hashlib
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
objects: dict[str, str] = {}


def ident(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:24].upper()


def add(name: str, body: str) -> str:
    key = ident(name)
    objects[key] = "{ " + body + " };"
    return key


def array(values: list[str]) -> str:
    return "( " + ", ".join(values) + (", " if values else "") + ")"


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


refs: list[str] = []
builds: list[str] = []
for path in sorted((ROOT / "JarvisVoice").rglob("*.swift")):
    relative = str(path.relative_to(ROOT))
    ref = add(
        relative,
        f"isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {quote(relative)}; sourceTree = SOURCE_ROOT;",
    )
    refs.append(ref)
    builds.append(add("build:" + relative, f"isa = PBXBuildFile; fileRef = {ref};"))
info = add(
    "info",
    'isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = "JarvisVoice/Info.plist"; sourceTree = SOURCE_ROOT;',
)
assets = add(
    "assets",
    'isa = PBXFileReference; lastKnownFileType = folder.assetcatalog; path = "JarvisVoice/Assets.xcassets"; sourceTree = SOURCE_ROOT;',
)
asset_build = add("asset_build", f"isa = PBXBuildFile; fileRef = {assets};")
model_refs: list[str] = []
model_builds: list[str] = []
for model in sorted((ROOT / "JarvisVoice/WakeWord/Models").glob("*.mlpackage")):
    relative = str(model.relative_to(ROOT))
    ref = add(relative, f"isa = PBXFileReference; lastKnownFileType = folder.mlpackage; path = {quote(relative)}; sourceTree = SOURCE_ROOT;")
    model_refs.append(ref)
    model_builds.append(add("build:" + relative, f"isa = PBXBuildFile; fileRef = {ref};"))
product = add(
    "product",
    "isa = PBXFileReference; explicitFileType = wrapper.application; path = JarvisVoice.app; sourceTree = BUILT_PRODUCTS_DIR;",
)
test_ref = add(
    "test_ref",
    'isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = "Tests/JarvisVoiceTests.swift"; sourceTree = SOURCE_ROOT;',
)
test_build = add("test_build", f"isa = PBXBuildFile; fileRef = {test_ref};")
test_product = add(
    "test_product",
    "isa = PBXFileReference; explicitFileType = wrapper.cfbundle; path = JarvisVoiceTests.xctest; sourceTree = BUILT_PRODUCTS_DIR;",
)
products = add(
    "products",
    f'isa = PBXGroup; children = {array([product, test_product])}; name = Products; sourceTree = "<group>";',
)
# Keep the Watch project external so its sources/protocol remain single-owned.
watch_root = ROOT.parents[1] / "watchos" / "JarvisWatch"
watch_project_file = watch_root / "JarvisWatch.xcodeproj" / "project.pbxproj"
watch_project = plistlib.loads(watch_project_file.read_bytes())
watch_objects = watch_project["objects"]
watch_target_id, watch_target = next(
    (key, value)
    for key, value in watch_objects.items()
    if value.get("isa") == "PBXNativeTarget" and value.get("name") == "JarvisWatch"
)
watch_project_ref = add(
    "watch_project",
    'isa = PBXFileReference; lastKnownFileType = "wrapper.pb-project"; path = "../../watchos/JarvisWatch/JarvisWatch.xcodeproj"; sourceTree = SOURCE_ROOT;',
)
watch_product_proxy = add(
    "watch_product_proxy",
    f'isa = PBXContainerItemProxy; containerPortal = {watch_project_ref}; proxyType = 2; remoteGlobalIDString = {watch_target["productReference"]}; remoteInfo = JarvisWatch;',
)
watch_product = add(
    "watch_product",
    f'isa = PBXReferenceProxy; fileType = wrapper.application; path = JarvisWatch.app; remoteRef = {watch_product_proxy}; sourceTree = BUILT_PRODUCTS_DIR;',
)
watch_products = add(
    "watch_products",
    f'isa = PBXGroup; children = {array([watch_product])}; name = Products; sourceTree = "<group>";',
)
watch_target_proxy = add(
    "watch_target_proxy",
    f"isa = PBXContainerItemProxy; containerPortal = {watch_project_ref}; proxyType = 1; remoteGlobalIDString = {watch_target_id}; remoteInfo = JarvisWatch;",
)
watch_dependency = add(
    "watch_dependency",
    f"isa = PBXTargetDependency; name = JarvisWatch; targetProxy = {watch_target_proxy};",
)
watch_build = add(
    "watch_build",
    f"isa = PBXBuildFile; fileRef = {watch_product}; settings = {{ ATTRIBUTES = (RemoveHeadersOnCopy, ); }};",
)
embed_watch = add(
    "embed_watch",
    f'isa = PBXCopyFilesBuildPhase; buildActionMask = 2147483647; dstPath = "$(CONTENTS_FOLDER_PATH)/Watch"; dstSubfolderSpec = 16; files = {array([watch_build])}; name = "Embed Watch Content"; runOnlyForDeploymentPostprocessing = 0;',
)
main = add(
    "main",
    f'isa = PBXGroup; children = {array(refs + model_refs + [test_ref, info, assets, watch_project_ref, products])}; sourceTree = "<group>";',
)
sources = add(
    "sources",
    f"isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = {array(builds)}; runOnlyForDeploymentPostprocessing = 0;",
)
resources = add(
    "resources",
    f"isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = {array([asset_build] + model_builds)}; runOnlyForDeploymentPostprocessing = 0;",
)
frameworks = add(
    "frameworks",
    "isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;",
)
project_configs: list[str] = []
target_configs: list[str] = []
for mode in ["Debug", "Release"]:
    project_settings = {
        "CLANG_ENABLE_MODULES": "YES",
        "CLANG_ENABLE_OBJC_ARC": "YES",
        "IPHONEOS_DEPLOYMENT_TARGET": "18.0",
        "SDKROOT": "iphoneos",
        "SWIFT_VERSION": "5.0",
        "SWIFT_STRICT_CONCURRENCY": "targeted",
        "DEBUG_INFORMATION_FORMAT": "dwarf" if mode == "Debug" else "dwarf-with-dsym",
        "SWIFT_OPTIMIZATION_LEVEL": "-Onone" if mode == "Debug" else "-O",
        "ENABLE_TESTABILITY": "YES" if mode == "Debug" else "NO",
        "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG" if mode == "Debug" else "",
    }
    target_settings = {
        "PRODUCT_BUNDLE_IDENTIFIER": "app.jarvis.JarvisVoice",
        "PRODUCT_NAME": "$(TARGET_NAME)",
        "MARKETING_VERSION": "1.0.0",
        "CURRENT_PROJECT_VERSION": "1",
        "CODE_SIGN_STYLE": "Automatic",
        "DEVELOPMENT_TEAM": "",
        "INFOPLIST_FILE": "JarvisVoice/Info.plist",
        "GENERATE_INFOPLIST_FILE": "NO",
        "TARGETED_DEVICE_FAMILY": "1",
        "SUPPORTED_PLATFORMS": "iphoneos iphonesimulator",
        "SUPPORTS_MACCATALYST": "NO",
        "ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon",
        "LD_RUNPATH_SEARCH_PATHS": "$(inherited) @executable_path/Frameworks",
    }

    def config(name: str, values: dict[str, str], configuration: str = mode) -> str:
        fields = " ".join(f"{key} = {quote(value)};" for key, value in values.items())
        return add(
            name,
            f"isa = XCBuildConfiguration; buildSettings = {{ {fields} }}; name = {configuration};",
        )

    project_configs.append(config("project:" + mode, project_settings))
    target_configs.append(config("target:" + mode, target_settings))
project_list = add(
    "project_list",
    f"isa = XCConfigurationList; buildConfigurations = {array(project_configs)}; defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;",
)
target_list = add(
    "target_list",
    f"isa = XCConfigurationList; buildConfigurations = {array(target_configs)}; defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;",
)
target = add(
    "target",
    f'isa = PBXNativeTarget; buildConfigurationList = {target_list}; buildPhases = {array([sources, frameworks, resources, embed_watch])}; buildRules = (); dependencies = {array([watch_dependency])}; name = JarvisVoice; productName = JarvisVoice; productReference = {product}; productType = "com.apple.product-type.application";',
)
test_sources = add(
    "test_sources",
    f"isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = {array([test_build])}; runOnlyForDeploymentPostprocessing = 0;",
)
test_configs: list[str] = []
for mode in ["Debug", "Release"]:
    settings = {
        "PRODUCT_BUNDLE_IDENTIFIER": "app.jarvis.JarvisVoiceTests",
        "PRODUCT_NAME": "$(TARGET_NAME)",
        "GENERATE_INFOPLIST_FILE": "YES",
        "CODE_SIGN_STYLE": "Automatic",
        "DEVELOPMENT_TEAM": "",
        "TEST_HOST": "$(BUILT_PRODUCTS_DIR)/JarvisVoice.app/$(BUNDLE_EXECUTABLE_FOLDER_PATH)/JarvisVoice",
        "BUNDLE_LOADER": "$(TEST_HOST)",
        "TARGETED_DEVICE_FAMILY": "1",
    }
    fields = " ".join(f"{key} = {quote(value)};" for key, value in settings.items())
    test_configs.append(
        add(
            "testconfig:" + mode,
            f"isa = XCBuildConfiguration; buildSettings = {{ {fields} }}; name = {mode};",
        )
    )
test_list = add(
    "test_list",
    f"isa = XCConfigurationList; buildConfigurations = {array(test_configs)}; defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;",
)
proxy = add(
    "test_proxy",
    f"isa = PBXContainerItemProxy; containerPortal = {ident('project')}; proxyType = 1; remoteGlobalIDString = {target}; remoteInfo = JarvisVoice;",
)
dependency = add(
    "test_dependency", f"isa = PBXTargetDependency; target = {target}; targetProxy = {proxy};"
)
test_target = add(
    "test_target",
    f'isa = PBXNativeTarget; buildConfigurationList = {test_list}; buildPhases = {array([test_sources])}; buildRules = (); dependencies = {array([dependency])}; name = JarvisVoiceTests; productName = JarvisVoiceTests; productReference = {test_product}; productType = "com.apple.product-type.bundle.unit-test";',
)
project = add(
    "project",
    f'isa = PBXProject; attributes = {{ BuildIndependentTargetsInParallel = YES; LastUpgradeCheck = 1600; TargetAttributes = {{ {target} = {{ CreatedOnToolsVersion = 16.0; }}; }}; }}; buildConfigurationList = {project_list}; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en, Base, "zh-Hans",); mainGroup = {main}; productRefGroup = {products}; projectDirPath = ""; projectRoot = ""; projectReferences = ({{ ProductGroup = {watch_products}; ProjectRef = {watch_project_ref}; }},); targets = {array([target, test_target])};',
)
folder = ROOT / "JarvisVoice.xcodeproj"
folder.mkdir(exist_ok=True)
(folder / "project.pbxproj").write_text(
    "// !$*UTF8*$!\n{ archiveVersion = 1; classes = {}; objectVersion = 56; objects = {\n"
    + "\n".join(f"{key} = {value}" for key, value in sorted(objects.items()))
    + f"\n}}; rootObject = {project}; }}\n"
)
schemes = folder / "xcshareddata/xcschemes"
schemes.mkdir(parents=True, exist_ok=True)
reference = f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target}" BuildableName="JarvisVoice.app" BlueprintName="JarvisVoice" ReferencedContainer="container:JarvisVoice.xcodeproj"/>'
(schemes / "JarvisVoice.xcscheme").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="1600" version="1.3">
<BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{reference}</BuildActionEntry></BuildActionEntries></BuildAction>
<TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv="YES"><Testables><TestableReference skipped="NO"><BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{test_target}" BuildableName="JarvisVoiceTests.xctest" BlueprintName="JarvisVoiceTests" ReferencedContainer="container:JarvisVoice.xcodeproj"/></TestableReference></Testables></TestAction>
<LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></LaunchAction>
<ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></ProfileAction>
<AnalyzeAction buildConfiguration="Debug"/><ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>""")
workspace = folder / "project.xcworkspace"
workspace.mkdir(exist_ok=True)
(workspace / "contents.xcworkspacedata").write_text(
    '<?xml version="1.0" encoding="UTF-8"?><Workspace version="1.0"><FileRef location="self:"/></Workspace>'
)
print(f"Generated {folder}: {len(refs)} Swift sources")
