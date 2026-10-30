import Foundation

/// A link the app was asked to open: a copied file or worker link
/// (`https://<server>/f/<path>`, `https://<server>/w/<worker>`, a universal link the server's
/// apple-app-site-association names this app for, crates/amux-server/src/api/
/// deep_links.rs) or the app's own scheme (`amux://open?server=<origin>#<hash>`,
/// `amux://file?server=<origin>&path=<path>`). It names the server to open and
/// the page to load there, with the file open.
struct DeepLink: Equatable {
    let server: URL
    let load: URL

    /// The server a link names, as `scheme://host[:port]`.
    static func origin(of url: URL) -> URL? {
        guard let scheme = url.scheme?.lowercased(), scheme == "https" || scheme == "http",
              let host = url.host, !host.isEmpty else { return nil }
        var c = URLComponents()
        c.scheme = scheme
        c.host = host
        c.port = url.port
        return c.url
    }

    /// `/#path=` with the dashboard's escaping (`_encodeHashPath`), so its
    /// router reads the path back exactly.
    static func hashPath(_ path: String) -> String {
        let p = path.hasPrefix("/") ? path : "/" + path
        return "#path=" + p.replacingOccurrences(of: "%", with: "%25").replacingOccurrences(of: "#", with: "%23")
            .replacingOccurrences(of: "?", with: "%3F").replacingOccurrences(of: " ", with: "%20")
    }

    static func page(_ server: URL, _ hash: String) -> URL? {
        URL(string: server.absoluteString.replacingOccurrences(of: "/+$", with: "", options: .regularExpression) + "/" + hash)
    }

    static func parse(_ url: URL) -> DeepLink? {
        let scheme = url.scheme?.lowercased() ?? ""
        if scheme == "https" || scheme == "http" {
            guard let server = origin(of: url) else { return nil }
            if url.path.hasPrefix("/w/") {
                let name = String(url.path.dropFirst(3))
                let ok = !name.isEmpty && name.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) && $0.isASCII || "._-".unicodeScalars.contains($0) }
                guard ok, let load = page(server, "#peek=" + name) else { return nil }
                return DeepLink(server: server, load: load)
            }
            // url.path is percent-decoded: /f/home/amux/a b.md -> /home/amux/a b.md
            guard url.path.hasPrefix("/f/") else { return nil }
            let file = String(url.path.dropFirst(2))
            guard file.count > 1, let load = page(server, hashPath(file)) else { return nil }
            return DeepLink(server: server, load: load)
        }
        guard scheme == "amux" else { return nil }
        let q = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        guard let s = q.first(where: { $0.name == "server" })?.value, let given = URL(string: s),
              let server = origin(of: given) else { return nil }
        switch url.host ?? "" {
        case "open":
            guard let load = page(server, url.fragment.map { "#" + $0 } ?? "") else { return nil }
            return DeepLink(server: server, load: load)
        case "file":
            guard let file = q.first(where: { $0.name == "path" })?.value, !file.isEmpty,
                  let load = page(server, hashPath(file)) else { return nil }
            return DeepLink(server: server, load: load)
        default:
            return nil
        }
    }
}
