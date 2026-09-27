// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ThreeDFM",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "ThreeDFM",
            path: "Sources/ThreeDFM"
        )
    ]
)
