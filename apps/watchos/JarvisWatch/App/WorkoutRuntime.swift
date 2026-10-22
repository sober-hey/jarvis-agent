import Combine
import Foundation
import HealthKit
import LocalAuthentication

/// An explicitly user-started workout, not a hidden keep-alive mechanism.
@MainActor
final class WorkoutRuntime: NSObject, ObservableObject, HKWorkoutSessionDelegate {
    @Published private(set) var running = false
    @Published private(set) var lastHeartRateAt: Date?
    @Published private(set) var wearEvidence = "unknown"
    @Published private(set) var authenticatedAt: Date?
    var onStateChange: (() -> Void)?
    var onError: ((String) -> Void)?
    private let store = HKHealthStore()
    private var session: HKWorkoutSession?
    private var query: HKAnchoredObjectQuery?
    private var authContext: LAContext?
    private var starting = false
    enum RuntimeError: Error { case unavailable, alreadyStarting }

    func start() async throws {
        guard !starting, session == nil else { throw RuntimeError.alreadyStarting }
        starting = true
        defer { starting = false }
        guard HKHealthStore.isHealthDataAvailable(),
              let heartRate = HKQuantityType.quantityType(forIdentifier: .heartRate) else {
            throw RuntimeError.unavailable
        }
        try await store.requestAuthorization(toShare: [HKObjectType.workoutType()], read: [heartRate])
        let configuration = HKWorkoutConfiguration()
        configuration.activityType = .other
        configuration.locationType = .unknown
        let session = try HKWorkoutSession(healthStore: store, configuration: configuration)
        session.delegate = self
        self.session = session
        session.startActivity(with: Date())
        let predicate = HKQuery.predicateForSamples(withStart: Date(), end: nil, options: .strictStartDate)
        let query = HKAnchoredObjectQuery(type: heartRate, predicate: predicate, anchor: nil,
                                         limit: HKObjectQueryNoLimit) { [weak self] _, samples, _, _, _ in
            let newest = samples?.map(\.endDate).max()
            Task { @MainActor in self?.updateEvidence(newest) }
        }
        query.updateHandler = { [weak self] _, samples, _, _, _ in
            let newest = samples?.map(\.endDate).max()
            Task { @MainActor in self?.updateEvidence(newest) }
        }
        self.query = query
        store.execute(query)
    }

    private func updateEvidence(_ date: Date?) {
        guard session != nil, let date else { return }
        lastHeartRateAt = max(lastHeartRateAt ?? .distantPast, date)
        onStateChange?()
    }

    /// Explicit authentication only. Failure never means "not worn".
    func verifyWear() async {
        guard authContext == nil else { return }
        let context = LAContext()
        authContext = context
        defer { context.invalidate(); authContext = nil }
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithWristDetection, error: &error) else {
            wearEvidence = "unknown"
            authenticatedAt = nil
            onStateChange?()
            return
        }
        do {
            if try await context.evaluatePolicy(.deviceOwnerAuthenticationWithWristDetection,
                                                 localizedReason: "验证手表佩戴状态") {
                authenticatedAt = Date()
                wearEvidence = "authenticated_on_wrist_at_timestamp"
            }
        } catch {
            authenticatedAt = nil
            wearEvidence = "unknown"
        }
        onStateChange?()
    }

    func stop() {
        let previous = session
        session = nil
        previous?.end()
        if let query { store.stop(query) }
        query = nil
        running = false
        lastHeartRateAt = nil
        onStateChange?()
    }

    nonisolated func workoutSession(_ workoutSession: HKWorkoutSession, didChangeTo toState: HKWorkoutSessionState,
                                   from fromState: HKWorkoutSessionState, date: Date) {
        Task { @MainActor in
            guard self.session === workoutSession else { return }
            self.running = toState == .running
            if toState == .ended || toState == .stopped { self.stop() }
            self.onStateChange?()
        }
    }
    nonisolated func workoutSession(_ workoutSession: HKWorkoutSession, didFailWithError error: Error) {
        Task { @MainActor in
            guard self.session === workoutSession else { return }
            self.stop()
            self.onError?("体能训练已终止：\(error.localizedDescription)")
        }
    }
}
