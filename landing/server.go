// Public static landing surface (MADR-0031, issue #427).
//
// A minimal stdlib-only static server: it serves EXACTLY two assets — the
// landing page HTML and its stylesheet — with pinned security headers, and
// refuses everything else. Zero env reads, zero config, zero cookies, zero
// redirects, no console path space (/auth/*, /api/*, /health* → 404).
// Any growth beyond static files requires a new MADR (MADR-0031 C7).
package main

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"log"
	"net/http"
	"time"
)

//go:embed landing.html static/landing.css
var assets embed.FS

// canonicalConsoleLoginURL is the one and only CTA target (MADR-0031 C6/C7):
// the console's canonical origin as a CONSTANT — never derived from a request
// (no Host/query influence ⇒ no open-redirect surface). The build stage
// rewrites the source HTML's relative href to exactly this URL and asserts it
// fail-closed; main() re-asserts the embedded artifact before serving.
const canonicalConsoleLoginURL = "https://coach-web-afff35tqxq-oa.a.run.app/auth/login"

// Static value-free content, zero cookies (MADR-0031 C5): short public caching
// is admissible and documented in docs/security.md — no shared cache sits in
// the path (no CDN; IAP is documented as incompatible with Cloud CDN) and the
// response is identical for every visitor.
var landingHeaders = map[string]string{
	"Cache-Control":           "public, max-age=300",
	"Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
	"X-Content-Type-Options":  "nosniff",
	"Referrer-Policy":         "no-referrer",
}

func notFound(w http.ResponseWriter) {
	applyLandingHeaders(w)
	// The error bodies are never cacheable: the explicit no-store override
	// must come after applyLandingHeaders to win.
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusNotFound)
	_, _ = w.Write([]byte("404 page not found\n"))
}

func methodNotAllowed(w http.ResponseWriter) {
	applyLandingHeaders(w)
	w.Header().Set("Allow", "GET, HEAD")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusMethodNotAllowed)
	_, _ = w.Write([]byte("405 method not allowed\n"))
}

// applyLandingHeaders sets the pinned security headers on every response (the
// 404/405 bodies are cacheable-never and get the explicit no-store override).
func applyLandingHeaders(w http.ResponseWriter) {
	for key, value := range landingHeaders {
		w.Header().Set(key, value)
	}
}

func landingHandler(html, css []byte) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			methodNotAllowed(w)
			return
		}
		switch r.URL.Path {
		case "/":
			applyLandingHeaders(w)
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write(html)
		case "/static/landing.css":
			applyLandingHeaders(w)
			w.Header().Set("Content-Type", "text/css; charset=utf-8")
			_, _ = w.Write(css)
		default:
			notFound(w)
		}
	})
}

func main() {
	html, err := assets.ReadFile("landing.html")
	if err != nil {
		log.Fatalf("landing: embedded landing.html missing: %v", err)
	}
	css, err := assets.ReadFile("static/landing.css")
	if err != nil {
		log.Fatalf("landing: embedded static/landing.css missing: %v", err)
	}
	// MADR-0031 C7 fail-closed: refuse to boot without the canonical CTA and
	// with any relative console href left in the embedded artifact.
	if !bytes.Contains(html, []byte(`href="`+canonicalConsoleLoginURL+`"`)) {
		log.Fatalf("landing: embedded HTML lacks href=%q — refusing to serve a broken CTA", `href="`+canonicalConsoleLoginURL+`"`)
	}
	if bytes.Contains(html, []byte(`href="/auth/login"`)) {
		log.Fatalf("landing: embedded HTML still carries the relative href=%q — refusing to serve", `href="/auth/login"`)
	}
	// Content provenance (C5/C6): log the served content hash so operators can
	// compare the running instance against the release-verified build log.
	sum := sha256.Sum256(html)
	log.Printf("landing: serving %d bytes, content sha256: %s", len(html), hex.EncodeToString(sum[:]))

	server := &http.Server{
		Addr:              ":8080",
		Handler:           landingHandler(html, css),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       30 * time.Second,
	}
	log.Printf("landing: listening on :8080")
	log.Fatal(server.ListenAndServe())
}
