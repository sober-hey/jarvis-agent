import AVFoundation
import Foundation
import JarvisActivation
import JarvisWakeWord

struct Item: Decodable { let source_id: String; let category: String; let split: String; let audio: String; let error: String? }
struct Manifest: Decodable { let items: [Item] }
@main struct Replay {
    static func main() throws {
        let args=CommandLine.arguments
        guard args.count == 4 else { fatalError("wakeword-replay <root> <manifest> <report>") }
        let root=URL(fileURLWithPath: args[1]); let data=root.appendingPathComponent("data")
        let items=try JSONDecoder().decode(Manifest.self,from:Data(contentsOf:URL(fileURLWithPath:args[2]))).items
        let scorer=try JarvisWakeWordScorer(modelsDirectory:root.appendingPathComponent("models"))
        var rows=[[String:Any]]();var allTimes=[Double]()
        var validation=0
        do { _ = try scorer.score(samples: [0]); fatalError("bad length accepted") } catch { validation += 1 }
        do { _ = try scorer.score(samples: [Float](repeating: .nan,count:1280)); fatalError("nan accepted") } catch { validation += 1 }
        do { _ = try scorer.score(samples: [Float](repeating: 2,count:1280)); fatalError("out of range accepted") } catch { validation += 1 }
        for item in items where item.error == nil {
            let file=try AVAudioFile(forReading:data.appendingPathComponent(item.audio))
            guard file.processingFormat.sampleRate==16000, file.processingFormat.channelCount==1 else { fatalError("16k mono required") }
            let buffer=AVAudioPCMBuffer(pcmFormat:file.processingFormat,frameCapacity:AVAudioFrameCount(file.length))!
            try file.read(into:buffer)
            let pcm=Array(UnsafeBufferPointer(start:buffer.floatChannelData![0],count:Int(buffer.frameLength)))
            let samples=[Float](repeating:0,count:16000)+pcm+[Float](repeating:0,count:16000)
            scorer.reset();var probabilities=[Float]();var lastHit = -Double.infinity;var events=[Double]();var times=[Double]()
            for offset in stride(from:0,through:samples.count-1280,by:1280) {
                let start=ProcessInfo.processInfo.systemUptime
                let p=try scorer.score(samples:Array(samples[offset..<offset+1280]))
                let ms=(ProcessInfo.processInfo.systemUptime-start)*1000
                times.append(ms);allTimes.append(ms);probabilities.append(p)
                let end=Double(offset+1280)/16000-1
                if p>=0.8 && end-lastHit>=1.5 { events.append(end);lastHit=end }
            }
            rows.append(["source_id":item.source_id,"category":item.category,"split":item.split,"max_score":probabilities.max() ?? 0,"events":events,"audio_seconds":Double(pcm.count)/16000,"max_hop_ms":times.max() ?? 0,"scores":probabilities])
        }
        let sorted=allTimes.sorted();func q(_ frac:Double)->Double { sorted[min(sorted.count-1,Int(Double(sorted.count-1)*frac))] }
        // Verify the real existing WakeWordDetector emits wakeWord with this scorer.
        let positive=items.first { $0.category == "bare" && $0.error == nil }
        var contract:[String:Any]=["input_validation_checks":validation]
        if let positive=positive {
            let file=try AVAudioFile(forReading:data.appendingPathComponent(positive.audio));let b=AVAudioPCMBuffer(pcmFormat:file.processingFormat,frameCapacity:AVAudioFrameCount(file.length))!;try file.read(into:b)
            let input=[Float](repeating:0,count:16000)+Array(UnsafeBufferPointer(start:b.floatChannelData![0],count:Int(b.frameLength)))+[Float](repeating:0,count:16000)
            scorer.reset();let detector=WakeWordDetector(scorer:scorer);var eventCount=0
            for start in stride(from:0,to:input.count,by:160) { eventCount += try detector.process(samples:Array(input[start..<min(start+160,input.count)]),sampleRate:16000,timestamp:Double(start)/16000).filter { $0.kind == .wakeWord }.count }
            contract["detector_replay_events"]=eventCount;contract["source_id"]=positive.source_id
        }
        let result:[String:Any]=["items":rows,"threshold":0.8,"cooldown_seconds":1.5,"hop_seconds":0.08,"latency_ms":["p50":q(0.5),"p95":q(0.95),"p99":q(0.99),"max":sorted.last!],"contract_checks":contract]
        let output=try JSONSerialization.data(withJSONObject:result,options:[.prettyPrinted,.sortedKeys]);try output.write(to:URL(fileURLWithPath:args[3]))
        print("Replayed \(rows.count) clips; hop p50=\(q(0.5))ms p95=\(q(0.95))ms max=\(sorted.last!)ms; checks \(contract)")
    }
}
