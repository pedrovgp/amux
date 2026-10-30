//! Links to a file or a worker that open where the person is: the browser, or
//! the amux iOS app with it open (Ethan, 2026-10-26).
//!
//! A copied file link is `https://<this amux>/f/<path>`, a worker link
//! `https://<this amux>/w/<worker>`. In a browser they redirect to
//! `/#path=<path>` (a file preview) and `/#peek=<worker>`. Signed out, the door
//! keeps the destination through sign-in (cloud/desktop/door.py, `next`).
//! On an iPhone or iPad with the amux app, iOS hands the same link to the app
//! instead (a universal link), because this server publishes
//! `/.well-known/apple-app-site-association` naming the app for `/f/*`. The
//! link is a real path, never only a `#` fragment, since iOS matches paths and
//! never sees a fragment. The Mac app is signed ad hoc, so macOS cannot hand it
//! https links; a browser on a Mac that arrived through one offers to open the
//! file in the Mac app (`amux://open?server=...#path=...`, app.js).
//!
//! The redirect and the association file reveal nothing: the dashboard behind
//! the redirect still needs the owner's sign-in, and the association file names
//! only the app.
use super::AppState;
use axum::extract::Path as AxPath;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::json;

/// The amux iOS app (ios/project.yml: team M225B6YA42, bundle
/// com.EthanSteininger.nextup). `AMUX_APPLE_APP_IDS` (comma separated,
/// TEAMID.bundle) replaces it for a server that ships its own build.
const DEFAULT_APP_ID: &str = "M225B6YA42.com.EthanSteininger.nextup";

/// Every route here answers without a sign-in (see the module doc).
pub fn public_routes() -> Router<AppState> {
    Router::new()
        .route("/.well-known/apple-app-site-association", get(apple_app_site_association))
        .route("/apple-app-site-association", get(apple_app_site_association))
        .route("/f/{*path}", get(file_link))
        .route("/w/{name}", get(worker_link))
}

/// The owner-only half: the address copied links are built on.
pub fn routes() -> Router<AppState> {
    Router::new().route("/api/deep-link", get(base))
}

fn app_ids() -> Vec<String> {
    let home = crate::config::amux_home();
    let ids: Vec<String> = crate::api::settings::effective_env(&home, "AMUX_APPLE_APP_IDS")
        .unwrap_or_default()
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| s.split_once('.').is_some_and(|(team, bundle)| team.len() == 10 && !bundle.is_empty()))
        .collect();
    if ids.is_empty() { vec![DEFAULT_APP_ID.to_string()] } else { ids }
}

async fn apple_app_site_association() -> Response {
    let body = json!({
        "applinks": {
            "details": [{
                "appIDs": app_ids(),
                "components": [
                    { "/": "/f/*", "comment": "a file in this amux, opened in the app" },
                    { "/": "/w/*", "comment": "a worker in this amux, opened in the app" },
                ],
            }],
        },
    });
    let mut r = Json(body).into_response();
    r.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("public, max-age=3600"));
    r
}

/// `/#path=` with the same escaping as the dashboard's `_encodeHashPath`, so
/// its router reads the path back exactly.
pub(crate) fn hash_for(path: &str) -> String {
    let enc = path.replace('%', "%25").replace('#', "%23").replace('?', "%3F").replace(' ', "%20");
    format!("/?via=link#path={enc}")
}

async fn file_link(AxPath(path): AxPath<String>) -> Response {
    let path = format!("/{}", path.trim_start_matches('/'));
    if path.contains('\0') || path.contains('\n') || path.contains('\r') {
        return (StatusCode::BAD_REQUEST, "not a file path\n").into_response();
    }
    tracing::info!(measured = true, n_considered = 1, verdict = "file_deep_link_opened",
        "a copied file link was opened in a browser (an app that claims it never reaches the server)");
    let mut r = (StatusCode::FOUND, [(header::LOCATION, hash_for(&path))]).into_response();
    r.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    r
}

/// A worker name as the dashboard writes it (letters, digits, `.`, `_`, `-`).
pub(crate) fn worker_hash(name: &str) -> Option<String> {
    let ok = !name.is_empty() && name.len() <= 128 && name.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b));
    ok.then(|| format!("/?via=link#peek={name}"))
}

async fn worker_link(AxPath(name): AxPath<String>) -> Response {
    let Some(to) = worker_hash(&name) else {
        return (StatusCode::NOT_FOUND, "not a worker name\n").into_response();
    };
    tracing::info!(measured = true, n_considered = 1, verdict = "worker_deep_link_opened",
        "a copied worker link was opened in a browser");
    let mut r = (StatusCode::FOUND, [(header::LOCATION, to)]).into_response();
    r.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    r
}

/// `{base}`: this amux's public address when known (AMUX_PUBLIC_URL, or the
/// one the owner opens the dashboard at), so a link copied inside an app
/// pointed at an IP still names the address others can open.
async fn base() -> Response {
    let home = crate::config::amux_home();
    let base = super::static_files::public_links::public_url(&home);
    Json(json!({ "base": base, "file_prefix": "/f", "worker_prefix": "/w", "measured": true, "n_considered": 1 })).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_redirect_escapes_what_the_dashboard_router_decodes() {
        assert_eq!(hash_for("/home/amux/a b#1?.md"), "/?via=link#path=/home/amux/a%20b%231%3F.md");
        assert_eq!(hash_for("/x/100%.txt"), "/?via=link#path=/x/100%25.txt");
    }

    #[test]
    fn a_worker_link_takes_only_a_worker_name() {
        assert_eq!(worker_hash("mtp-demo").as_deref(), Some("/?via=link#peek=mtp-demo"));
        for bad in ["", "a b", "x#y", "../x", "<s>", "a&tab=x"] {
            assert_eq!(worker_hash(bad), None, "{bad}");
        }
    }

    #[test]
    fn an_app_id_is_team_dot_bundle_or_the_default_stands() {
        assert_eq!(app_ids(), vec![DEFAULT_APP_ID.to_string()]);
        assert!(DEFAULT_APP_ID.split_once('.').is_some_and(|(t, _)| t.len() == 10));
    }
}
