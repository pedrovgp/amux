import XCTest

/// Copied file links open the file in the app (DeepLink.swift).
final class DeepLinkTests: XCTestCase {
    func testAUniversalFileLinkOpensThatFileOnThatServer() {
        let l = DeepLink.parse(URL(string: "https://sandbox.amux.io/f/home/amux/rothco/deck%20v2.html")!)
        XCTAssertEqual(l?.server.absoluteString, "https://sandbox.amux.io")
        XCTAssertEqual(l?.load.absoluteString, "https://sandbox.amux.io/#path=/home/amux/rothco/deck%20v2.html")
    }

    func testThePortIsKeptAndHashAndQuestionMarksStayInThePath() {
        let l = DeepLink.parse(URL(string: "https://100.1.2.3:8824/f/x/a%23b%3Fc.md")!)
        XCTAssertEqual(l?.server.absoluteString, "https://100.1.2.3:8824")
        XCTAssertEqual(l?.load.absoluteString, "https://100.1.2.3:8824/#path=/x/a%23b%3Fc.md")
    }

    func testTheAppSchemeOpensAHashOrAFile() {
        let open = DeepLink.parse(URL(string: "amux://open?server=https%3A%2F%2Fsandbox.amux.io#path=/home/amux/a.md")!)
        XCTAssertEqual(open?.load.absoluteString, "https://sandbox.amux.io/#path=/home/amux/a.md")
        let file = DeepLink.parse(URL(string: "amux://file?server=https://sandbox.amux.io&path=/home/amux/b%20c.md")!)
        XCTAssertEqual(file?.load.absoluteString, "https://sandbox.amux.io/#path=/home/amux/b%20c.md")
    }

    func testAWorkerLinkOpensThatWorker() {
        let l = DeepLink.parse(URL(string: "https://sandbox.amux.io/w/mtp-demo")!)
        XCTAssertEqual(l?.load.absoluteString, "https://sandbox.amux.io/#peek=mtp-demo")
        XCTAssertNil(DeepLink.parse(URL(string: "https://sandbox.amux.io/w/a%26tab=x")!))
        XCTAssertNil(DeepLink.parse(URL(string: "https://sandbox.amux.io/w/")!))
    }

    func testAnythingElseIsNotALink() {
        for s in ["https://sandbox.amux.io/", "https://sandbox.amux.io/pub/abc/", "https://sandbox.amux.io/f/",
                  "amux://open", "amux://open?server=javascript:alert(1)", "amux://file?server=file:///etc&path=/x",
                  "ftp://x/f/y"] {
            XCTAssertNil(DeepLink.parse(URL(string: s)!), s)
        }
    }
}
