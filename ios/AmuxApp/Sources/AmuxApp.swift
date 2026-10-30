import SwiftUI

@main
struct AmuxApp: App {
    @StateObject private var serverManager = ServerManager()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        LocationRecorder.shared.resumeIfEnabled()
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if serverManager.hasServer {
                    ContentView()
                        .environmentObject(serverManager)
                } else {
                    ServerPickerView()
                        .environmentObject(serverManager)
                }
            }
            .preferredColorScheme(.dark)
            // A copied file link (https://<server>/f/<path>, or amux://) opens
            // that file here (DeepLink.swift). onOpenURL receives both the app's
            // scheme and universal links; the activity handler is the same path
            // for a universal link delivered as a browsing activity.
            .onOpenURL { serverManager.open(deepLink: $0) }
            .onContinueUserActivity(NSUserActivityTypeBrowsingWeb) { activity in
                if let url = activity.webpageURL { serverManager.open(deepLink: url) }
            }
        }
        // Shares queued while offline (ShareOutbox) go out when the app opens
        // or comes back to the foreground, as well as on the next share.
        .onChange(of: scenePhase) { phase in
            if phase == .active { Task { await ShareOutbox.drainShared() } }
        }
    }
}
