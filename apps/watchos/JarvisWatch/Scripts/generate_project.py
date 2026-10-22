#!/usr/bin/env python3
"""Generate the checked-in, dependency-free Xcode project (not a build tool)."""

import hashlib
import plistlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
objects: dict[str, dict[str, Any]] = {}


def uid(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:24].upper()


def add(object_name: str, isa: str, **kwargs: Any) -> str:
    key = uid(object_name)
    objects[key] = dict(isa=isa, **kwargs)
    return key


def file(path: str, kind: str, group: str) -> str:
    reference = add(
        "file:" + path, "PBXFileReference", lastKnownFileType=kind, path=path, sourceTree="<group>"
    )
    objects[group]["children"].append(reference)
    return reference


def phase(name: str, isa: str, refs: list[str]) -> str:
    builds = [
        add("build:" + name + str(i), "PBXBuildFile", fileRef=ref) for i, ref in enumerate(refs)
    ]
    return add(
        name, isa, buildActionMask=2147483647, files=builds, runOnlyForDeploymentPostprocessing=0
    )


def configurations(name: str, settings: dict[str, Any]) -> str:
    entries: list[str] = []
    for config in ("Debug", "Release"):
        options = dict(settings)
        options.update(
            SWIFT_OPTIMIZATION_LEVEL="-Onone" if config == "Debug" else "-O",
            DEBUG_INFORMATION_FORMAT="dwarf" if config == "Debug" else "dwarf-with-dsym",
            ENABLE_TESTABILITY="YES" if config == "Debug" else "NO",
        )
        if config == "Debug":
            options["SWIFT_ACTIVE_COMPILATION_CONDITIONS"] = "DEBUG $(inherited)"
        entries.append(
            add(name + config, "XCBuildConfiguration", buildSettings=options, name=config)
        )
    return add(
        name + "configs",
        "XCConfigurationList",
        buildConfigurations=entries,
        defaultConfigurationIsVisible=0,
        defaultConfigurationName="Release",
    )


main = add("main", "PBXGroup", children=[], sourceTree="<group>")
products = add("products", "PBXGroup", children=[], name="Products", sourceTree="<group>")
sources = add("sources", "PBXGroup", children=[], name="Source", sourceTree="<group>")
tests = add("tests", "PBXGroup", children=[], name="Tests", sourceTree="<group>")
objects[main]["children"] += [sources, tests, products]
app_sources = [
    file(str(path.relative_to(ROOT)), "sourcecode.swift", sources)
    for folder in ("App", "Core")
    for path in sorted((ROOT / folder).glob("*.swift"))
]
test_sources = [
    file("Tests/" + name, "sourcecode.swift", tests)
    for name in ("WatchCoreSuite.swift", "WatchCoreTests.swift")
]
fixture = file("Tests/Fixtures/wisdm_1600_clapping_excerpt.txt", "text", tests)
for name in ("Info.plist", "JarvisWatch.entitlements"):
    file(name, "text.plist.xml", main)
file("README.md", "net.daringfireball.markdown", main)
app_product = add(
    "app-product",
    "PBXFileReference",
    explicitFileType="wrapper.application",
    includeInIndex=0,
    path="JarvisWatch.app",
    sourceTree="BUILT_PRODUCTS_DIR",
)
test_product = add(
    "test-product",
    "PBXFileReference",
    explicitFileType="wrapper.cfbundle",
    includeInIndex=0,
    path="JarvisWatchTests.xctest",
    sourceTree="BUILT_PRODUCTS_DIR",
)
objects[products]["children"] += [app_product, test_product]
project_settings = dict(
    CLANG_ENABLE_MODULES="YES",
    SWIFT_VERSION="5.0",
    SWIFT_STRICT_CONCURRENCY="targeted",
    WATCHOS_DEPLOYMENT_TARGET="11.0",
    SDKROOT="watchos",
    SUPPORTED_PLATFORMS="watchos watchsimulator",
    TARGETED_DEVICE_FAMILY="4",
    CODE_SIGN_STYLE="Automatic",
    DEVELOPMENT_TEAM="",
    ENABLE_USER_SCRIPT_SANDBOXING="YES",
)
app_settings = dict(
    WATCHOS_DEPLOYMENT_TARGET="11.0",
    PRODUCT_BUNDLE_IDENTIFIER="app.jarvis.JarvisVoice.watchkitapp",
    PRODUCT_NAME="$(TARGET_NAME)",
    INFOPLIST_FILE="Info.plist",
    GENERATE_INFOPLIST_FILE="NO",
    CODE_SIGN_ENTITLEMENTS="JarvisWatch.entitlements",
    CURRENT_PROJECT_VERSION="1",
    MARKETING_VERSION="1.0.0",
    SKIP_INSTALL="YES",
    LD_RUNPATH_SEARCH_PATHS=["$(inherited)", "@executable_path/Frameworks"],
    SWIFT_EMIT_LOC_STRINGS="YES",
)
app = add(
    "app",
    "PBXNativeTarget",
    buildConfigurationList=configurations("app", app_settings),
    buildPhases=[
        phase("app-sources", "PBXSourcesBuildPhase", app_sources),
        phase("app-frameworks", "PBXFrameworksBuildPhase", []),
        phase("app-resources", "PBXResourcesBuildPhase", []),
    ],
    buildRules=[],
    dependencies=[],
    name="JarvisWatch",
    productName="JarvisWatch",
    productReference=app_product,
    productType="com.apple.product-type.application",
)
proxy = add(
    "proxy",
    "PBXContainerItemProxy",
    containerPortal=uid("project"),
    proxyType=1,
    remoteGlobalIDString=app,
    remoteInfo="JarvisWatch",
)
dependency = add("dependency", "PBXTargetDependency", target=app, targetProxy=proxy)
test_settings = dict(
    WATCHOS_DEPLOYMENT_TARGET="11.0",
    PRODUCT_BUNDLE_IDENTIFIER="app.jarvis.watch.tests",
    PRODUCT_NAME="$(TARGET_NAME)",
    GENERATE_INFOPLIST_FILE="YES",
    TEST_TARGET_NAME="JarvisWatch",
    TEST_HOST="$(BUILT_PRODUCTS_DIR)/JarvisWatch.app/$(BUNDLE_EXECUTABLE_FOLDER_PATH)/JarvisWatch",
    BUNDLE_LOADER="$(TEST_HOST)",
    SKIP_INSTALL="YES",
    LD_RUNPATH_SEARCH_PATHS=[
        "$(inherited)",
        "@executable_path/Frameworks",
        "@loader_path/Frameworks",
    ],
)
test_target = add(
    "test",
    "PBXNativeTarget",
    buildConfigurationList=configurations("test", test_settings),
    buildPhases=[
        phase("test-sources", "PBXSourcesBuildPhase", test_sources),
        phase("test-frameworks", "PBXFrameworksBuildPhase", []),
        phase("test-resources", "PBXResourcesBuildPhase", [fixture]),
    ],
    buildRules=[],
    dependencies=[dependency],
    name="JarvisWatchTests",
    productName="JarvisWatchTests",
    productReference=test_product,
    productType="com.apple.product-type.bundle.unit-test",
)
project = add(
    "project",
    "PBXProject",
    attributes=dict(
        BuildIndependentTargetsInParallel="YES",
        LastUpgradeCheck="1600",
        TargetAttributes={
            app: dict(
                CreatedOnToolsVersion="16.0",
                SystemCapabilities={
                    "com.apple.HealthKit": {"enabled": 1},
                    "com.apple.BackgroundModes": {"enabled": 1},
                },
            ),
            test_target: dict(CreatedOnToolsVersion="16.0", TestTargetID=app),
        },
    ),
    buildConfigurationList=configurations("project", project_settings),
    compatibilityVersion="Xcode 14.0",
    developmentRegion="zh-Hans",
    hasScannedForEncodings=0,
    knownRegions=["zh-Hans", "en", "Base"],
    mainGroup=main,
    productRefGroup=products,
    projectDirPath="",
    projectRoot="",
    targets=[app, test_target],
)
folder = ROOT / "JarvisWatch.xcodeproj"
folder.mkdir(exist_ok=True)
# Xcode supports XML property lists as well as the conventional OpenStep syntax.
(folder / "project.pbxproj").write_bytes(
    plistlib.dumps(
        dict(
            archiveVersion="1", classes={}, objectVersion="56", objects=objects, rootObject=project
        ),
        sort_keys=False,
    )
)
workspace = folder / "project.xcworkspace"
workspace.mkdir(exist_ok=True)
(workspace / "contents.xcworkspacedata").write_text(
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Workspace version="1.0"><FileRef location="self:"/></Workspace>\n'
)
shared = folder / "xcshareddata/xcschemes"
shared.mkdir(parents=True, exist_ok=True)
scheme = ET.Element("Scheme", LastUpgradeVersion="1600", version="1.3")
build = ET.SubElement(
    scheme, "BuildAction", parallelizeBuildables="YES", buildImplicitDependencies="YES"
)
entries = ET.SubElement(build, "BuildActionEntries")


def reference(parent: ET.Element, target: str, name: str, product: str) -> None:
    ET.SubElement(
        parent,
        "BuildableReference",
        BuildableIdentifier="primary",
        BlueprintIdentifier=target,
        BuildableName=product,
        BlueprintName=name,
        ReferencedContainer="container:JarvisWatch.xcodeproj",
    )


entry = ET.SubElement(
    entries,
    "BuildActionEntry",
    buildForTesting="YES",
    buildForRunning="YES",
    buildForProfiling="YES",
    buildForArchiving="YES",
    buildForAnalyzing="YES",
)
reference(entry, app, "JarvisWatch", "JarvisWatch.app")
test = ET.SubElement(
    scheme,
    "TestAction",
    buildConfiguration="Debug",
    selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB",
    selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB",
    shouldUseLaunchSchemeArgsEnv="YES",
)
testables = ET.SubElement(test, "Testables")
reference(
    ET.SubElement(testables, "TestableReference", skipped="NO"),
    test_target,
    "JarvisWatchTests",
    "JarvisWatchTests.xctest",
)
launch = ET.SubElement(
    scheme,
    "LaunchAction",
    buildConfiguration="Debug",
    selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB",
    selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB",
    launchStyle="0",
    useCustomWorkingDirectory="NO",
    ignoresPersistentStateOnLaunch="NO",
    debugDocumentVersioning="YES",
    debugServiceExtension="internal",
    allowLocationSimulation="YES",
)
reference(
    ET.SubElement(launch, "BuildableProductRunnable", runnableDebuggingMode="0"),
    app,
    "JarvisWatch",
    "JarvisWatch.app",
)
profile = ET.SubElement(
    scheme,
    "ProfileAction",
    buildConfiguration="Release",
    shouldUseLaunchSchemeArgsEnv="YES",
    savedToolIdentifier="",
    useCustomWorkingDirectory="NO",
    debugDocumentVersioning="YES",
)
reference(
    ET.SubElement(profile, "BuildableProductRunnable", runnableDebuggingMode="0"),
    app,
    "JarvisWatch",
    "JarvisWatch.app",
)
ET.SubElement(scheme, "AnalyzeAction", buildConfiguration="Debug")
ET.SubElement(scheme, "ArchiveAction", buildConfiguration="Release", revealArchiveInOrganizer="YES")
ET.indent(scheme)
ET.ElementTree(scheme).write(
    shared / "JarvisWatch.xcscheme", encoding="UTF-8", xml_declaration=True
)
print("Generated JarvisWatch.xcodeproj with watchOS app + hosted XCTest target")
