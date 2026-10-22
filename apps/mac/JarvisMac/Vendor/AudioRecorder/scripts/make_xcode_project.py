#!/usr/bin/env python3
"""Generate an isolated Xcode CLI/test harness; never edits JarvisMacApp."""
import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
objects = {}
counter = 0

def add(value):
    global counter
    counter += 1
    key = f"{counter:024X}"
    objects[key] = value
    return key

def config_list(settings):
    configs = [add(dict(isa="XCBuildConfiguration", name=name, buildSettings=settings)) for name in ("Debug", "Release")]
    return add(dict(isa="XCConfigurationList", buildConfigurations=configs, defaultConfigurationIsVisible=0, defaultConfigurationName="Debug"))

sources = sorted((ROOT / "Sources").glob("*.swift"))
refs = {str(p.relative_to(ROOT)): add(dict(isa="PBXFileReference", lastKnownFileType="sourcecode.swift", path=str(p.relative_to(ROOT)), sourceTree="SOURCE_ROOT")) for p in sources + [ROOT / "Tests/main.swift", ROOT / "CLI/main.swift"]}
products = []
targets = []
for name, entry in [("AudioRecorderTests", "Tests/main.swift"), ("AudioRecorderCLI", "CLI/main.swift")]:
    files = [add(dict(isa="PBXBuildFile", fileRef=refs[str(p.relative_to(ROOT))])) for p in sources + [ROOT / entry]]
    phase = add(dict(isa="PBXSourcesBuildPhase", buildActionMask=2147483647, files=files, runOnlyForDeploymentPostprocessing=0))
    product = add(dict(isa="PBXFileReference", explicitFileType="compiled.mach-o.executable", path=name, sourceTree="BUILT_PRODUCTS_DIR"))
    products.append(product)
    settings = dict(PRODUCT_NAME=name, SWIFT_VERSION="5.0", MACOSX_DEPLOYMENT_TARGET="13.0", SDKROOT="macosx", CODE_SIGNING_ALLOWED="NO", SWIFT_OPTIMIZATION_LEVEL="-Onone", CLANG_ENABLE_MODULES="YES", ENABLE_HARDENED_RUNTIME="NO")
    if entry.startswith("CLI"):
        settings.update(INFOPLIST_FILE="Resources/Info.plist", CREATE_INFOPLIST_SECTION_IN_BINARY="YES")
    target = add(dict(isa="PBXNativeTarget", name=name, productName=name, productType="com.apple.product-type.tool", productReference=product, buildConfigurationList=config_list(settings), buildPhases=[phase], buildRules=[], dependencies=[]))
    targets.append(target)
product_group = add(dict(isa="PBXGroup", name="Products", children=products, sourceTree="<group>"))
group = add(dict(isa="PBXGroup", children=list(refs.values()) + [product_group], sourceTree="<group>"))
project = add(dict(isa="PBXProject", buildConfigurationList=config_list({}), compatibilityVersion="Xcode 14.0", developmentRegion="en", knownRegions=["en"], hasScannedForEncodings=0, mainGroup=group, productRefGroup=product_group, projectDirPath="", projectRoot="", targets=targets))

def plist(value):
    if isinstance(value, dict):
        return "{\n" + "\n".join(f"{plist(k)} = {plist(v)};" for k, v in value.items()) + "\n}"
    if isinstance(value, list):
        return "(" + ",".join(plist(v) for v in value) + (",)" if value else ")")
    if isinstance(value, int):
        return str(value)
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'

target = ROOT / "AudioRecorder.xcodeproj/project.pbxproj"
target.parent.mkdir(exist_ok=True)
target.write_text("// !$*UTF8*$!\n" + plist(dict(archiveVersion=1, classes={}, objectVersion=56, objects=objects, rootObject=project)))
print(target)
