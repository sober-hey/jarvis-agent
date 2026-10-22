import Foundation

/// Fixed-size byte ring; owner serializes access on the processing queue.
final class PCMBufferRing {
    private var storage: [UInt8]
    private var writeIndex = 0
    private var count = 0
    init(capacityBytes: Int) {
        precondition(capacityBytes > 0 && capacityBytes % 2 == 0)
        storage = .init(repeating: 0, count: capacityBytes)
    }
    func append(_ data: Data) {
        for byte in data.suffix(storage.count) {
            storage[writeIndex] = byte
            writeIndex = (writeIndex + 1) % storage.count
            count = min(count + 1, storage.count)
        }
    }
    func snapshot() -> Data {
        let start = (writeIndex - count + storage.count) % storage.count
        return Data((0..<count).map { storage[(start + $0) % storage.count] })
    }
    func reset() { writeIndex = 0; count = 0 }
}
