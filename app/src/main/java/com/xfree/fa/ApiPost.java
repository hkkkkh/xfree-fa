package com.xfree.fa;

import com.xfree.fa.api.Proxy;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * v2.4 — honest POST/GET helper for the site's cookie-based auth.
 *
 * Exact flow from the site's own bundle (6347c71.js, still live):
 *   POST /api/anonymous-login (no body) -> Set-Cookie: anonymous=<JWT>
 *   POST /api/login {email, password} with that cookie
 *        -> body {"statusCode": 2, "body": {"accessToken": "<JWT>"}}
 *        (statusCode -3 = banned, -4 = account pending deletion)
 *   GET  /api/user/<userId> (loadMyInfo) with the authorization cookie.
 * A login counts ONLY on HTTP 200 + statusCode 2 + a body.accessToken
 * that decodes as a JWT carrying a userId. Anything else is reported
 * with the real HTTP code and statusCode — never a faked success.
 * The login form's exact body fields are NOT in the downloaded bundle
 * (the lazy login.vue chunk wasn't captured); {email, password} is the
 * form object the v2.3 screen already used, kept unchanged.
 */
public final class ApiPost {
    private ApiPost() {}

    private static final String UA =
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36";

    public static final class Result {
        /** HTTP code from the target server, -1 when unreachable. */
        public final int httpCode;
        public final String body;
        /** "proxy" or "direct". */
        public final String via;
        public final String error;
        /** Raw upstream Set-Cookie header values (may be empty). */
        public final java.util.List<String> setCookies;
        Result(int httpCode, String body, String via, String error) {
            this(httpCode, body, via, error,
                    java.util.Collections.<String>emptyList());
        }
        Result(int httpCode, String body, String via, String error,
               java.util.List<String> setCookies) {
            this.httpCode = httpCode;
            this.body = body == null ? "" : body;
            this.via = via;
            this.error = error;
            this.setCookies = setCookies == null
                    ? java.util.Collections.<String>emptyList() : setCookies;
        }
        public boolean is2xx() { return httpCode >= 200 && httpCode < 300; }
        /** Value of a named cookie from the Set-Cookie headers, or null. */
        public String cookie(String name) {
            for (String sc : setCookies) {
                if (sc == null) continue;
                String t = sc.trim();
                if (t.startsWith(name + "=")) {
                    String v = t.substring(name.length() + 1);
                    int semi = v.indexOf(';');
                    if (semi >= 0) v = v.substring(0, semi);
                    return v;
                }
            }
            return null;
        }
    }

    private static String readBody(HttpURLConnection c) {
        try {
            InputStream in;
            try {
                in = c.getInputStream();
            } catch (Exception e) {
                in = c.getErrorStream();
            }
            if (in == null) return "";
            StringBuilder sb = new StringBuilder();
            try (BufferedReader r = new BufferedReader(
                    new InputStreamReader(in, StandardCharsets.UTF_8))) {
                char[] buf = new char[4096];
                int n;
                while ((n = r.read(buf)) != -1 && sb.length() < 20000) {
                    sb.append(buf, 0, n);
                }
            }
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    private static String shortErr(Exception e) {
        if (e instanceof java.net.SocketTimeoutException) return "timeout";
        if (e instanceof java.net.UnknownHostException) return "DNS";
        String m = e.getMessage();
        return m == null ? e.getClass().getSimpleName()
                : (m.length() > 60 ? m.substring(0, 60) : m);
    }

    /** Direct POST of a JSON body, optionally with a Cookie header.
     *  Set-Cookie headers are captured in the result. */
    public static Result postJsonDirect(String urlString, String json,
                                        int timeoutMs, String cookieHeader) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(urlString).openConnection();
            c.setConnectTimeout(timeoutMs);
            c.setReadTimeout(timeoutMs);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("User-Agent", UA);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Accept", "application/json, text/plain, */*");
            // Site's axios interceptor (6347c71.js): apiversion + language
            // ride on every /api call; without them the edge 404s.
            c.setRequestProperty("apiversion", "1.0");
            c.setRequestProperty("language", "en");
            c.setRequestProperty("Origin", "https://www.xfree.com");
            c.setRequestProperty("Referer", "https://www.xfree.com/");
            if (cookieHeader != null && !cookieHeader.isEmpty()) {
                c.setRequestProperty("Cookie", cookieHeader);
            }
            try (OutputStream out = c.getOutputStream()) {
                out.write(json.getBytes(StandardCharsets.UTF_8));
            }
            int code = c.getResponseCode();
            return new Result(code, readBody(c), "direct", null,
                    setCookieList(c));
        } catch (Exception e) {
            return new Result(-1, "", "direct", shortErr(e));
        } finally {
            if (c != null) c.disconnect();
        }
    }

    public static Result postJsonDirect(String urlString, String json, int timeoutMs) {
        return postJsonDirect(urlString, json, timeoutMs, null);
    }

    private static java.util.List<String> setCookieList(HttpURLConnection c) {
        java.util.List<String> out = new java.util.ArrayList<>();
        try {
            java.util.Map<String, java.util.List<String>> headers =
                    c.getHeaderFields();
            if (headers != null) {
                for (java.util.Map.Entry<String, java.util.List<String>> e
                        : headers.entrySet()) {
                    if (e.getKey() != null
                            && e.getKey().equalsIgnoreCase("Set-Cookie")
                            && e.getValue() != null) {
                        out.addAll(e.getValue());
                    }
                }
            }
        } catch (Exception ignored) {}
        return out;
    }

    /** Parse the proxy /auth relay envelope
     *  {"status": N, "body": "...", "setCookie": [...]}. */
    private static Result fromRelay(Result relay) {
        if (relay.httpCode == 404) {
            return new Result(-1, "", "proxy",
                    "proxy 404 — old proxy deploy, /auth not deployed yet");
        }
        if (!relay.is2xx()) {
            return new Result(relay.httpCode, relay.body, "proxy",
                    relay.error != null ? relay.error : "proxy HTTP " + relay.httpCode);
        }
        int status = extractInt(relay.body, "status");
        String body = extractString(relay.body, "body");
        java.util.List<String> cookies = new java.util.ArrayList<>();
        Matcher m = Pattern.compile("\"setCookie\"\\s*:\\s*\\[(.*?)\\]")
                .matcher(relay.body);
        if (m.find()) {
            Matcher s = Pattern.compile("\"((?:[^\"\\\\]|\\\\.)*)\"")
                    .matcher(m.group(1));
            while (s.find()) {
                cookies.add(s.group(1).replace("\\\"", "\"")
                        .replace("\\\\", "\\"));
            }
        }
        return new Result(status, body == null ? "" : body, "proxy", null,
                cookies);
    }

    /**
     * POST a JSON body through the own proxy's /auth relay
     * (proxy/app.py v2.4: POST /auth {"url": "...", "body": {...},
     * "cookies": "..."} -> {"status", "body", "setCookie"}).
     * The proxy answers {"status": <upstream code>, "body": "<text>"}.
     * An old proxy deploy answers 404 — reported, not hidden.
     */
    public static Result postJsonViaProxy(String targetUrl, String json,
                                          int timeoutMs, String cookieHeader) {
        String relayBody = "{\"url\":" + jsonQuote(targetUrl)
                + ",\"body\":" + json
                + ",\"cookies\":" + jsonQuote(cookieHeader == null ? "" : cookieHeader)
                + ",\"origin\":\"https://www.xfree.com\""
                + ",\"referer\":\"https://www.xfree.com/\"}";
        Result relay = postJsonDirect(Proxy.PROXY_BASE + "/auth", relayBody, timeoutMs);
        return fromRelay(relay);
    }

    public static Result postJsonViaProxy(String targetUrl, String json, int timeoutMs) {
        return postJsonViaProxy(targetUrl, json, timeoutMs, null);
    }

    /** Try proxy first, then direct, with a Cookie header.
     *  Returns the first attempt that got any HTTP answer. */
    public static Result postJson(String targetUrl, String json, String cookieHeader) {
        Result viaProxy = postJsonViaProxy(targetUrl, json, 30000, cookieHeader);
        if (viaProxy.httpCode != -1) return viaProxy;
        Result direct = postJsonDirect(targetUrl, json, 15000, cookieHeader);
        if (direct.httpCode != -1) return direct;
        return viaProxy;
    }

    public static Result postJson(String targetUrl, String json) {
        return postJson(targetUrl, json, null);
    }

    /** GET through the proxy's GET /auth relay (v2.4), then direct.
     *  Used for cookie-authenticated GETs — the site's loadMyInfo:
     *  GET /api/user/<userId>. */
    public static Result getJson(String targetUrl, String cookieHeader) {
        String relayUrl = Proxy.PROXY_BASE + "/auth?url="
                + java.net.URLEncoder.encode(targetUrl, StandardCharsets.UTF_8);
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(relayUrl).openConnection();
            c.setConnectTimeout(30000);
            c.setReadTimeout(30000);
            c.setRequestMethod("GET");
            c.setRequestProperty("User-Agent", UA);
            c.setRequestProperty("Accept", "application/json");
            if (cookieHeader != null && !cookieHeader.isEmpty()) {
                c.setRequestProperty("Cookie", cookieHeader);
            }
            int code = c.getResponseCode();
            Result relay = new Result(code, readBody(c), "proxy", null);
            if (code != 404) return fromRelay(relay);
        } catch (Exception ignored) {
        } finally {
            if (c != null) c.disconnect();
        }
        c = null;
        try {
            c = (HttpURLConnection) new URL(targetUrl).openConnection();
            c.setConnectTimeout(15000);
            c.setReadTimeout(15000);
            c.setRequestMethod("GET");
            c.setRequestProperty("User-Agent", UA);
            c.setRequestProperty("Accept", "application/json, text/plain, */*");
            c.setRequestProperty("apiversion", "1.0");
            c.setRequestProperty("language", "en");
            c.setRequestProperty("Origin", "https://www.xfree.com");
            c.setRequestProperty("Referer", "https://www.xfree.com/");
            if (cookieHeader != null && !cookieHeader.isEmpty()) {
                c.setRequestProperty("Cookie", cookieHeader);
            }
            int code = c.getResponseCode();
            return new Result(code, readBody(c), "direct", null,
                    setCookieList(c));
        } catch (Exception e) {
            return new Result(-1, "", "direct", shortErr(e));
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** The site's response envelope: {"statusCode": N, "body": {...}}.
     *  Success for login-family calls is statusCode 2 (proven by
     *  sendLogin's sibling calls in 6347c71.js: login-google,
     *  login-apple, apple-exchange-code and login-switch all test
     *  for 2). Returns -999 when no statusCode is present. */
    public static int extractStatusCode(String body) {
        if (body == null) return -999;
        Matcher m = Pattern.compile("\"statusCode\"\\s*:\\s*(-?\\d+)")
                .matcher(body);
        if (!m.find()) return -999;
        try { return Integer.parseInt(m.group(1)); } catch (Exception ignored) {}
        return -999;
    }

    /** The site's setLogin reads exactly body.accessToken
     *  (6347c71.js). No other token field is used for login. */
    public static String extractAccessToken(String body) {
        if (body == null) return null;
        Matcher m = Pattern.compile("\"accessToken\"\\s*:\\s*\"([^\"]{10,})\"")
                .matcher(body);
        return m.find() ? m.group(1) : null;
    }

    /** Decode the "userId" claim from a JWT payload, like the site's
     *  setLogin (it decodes the accessToken client-side). Null when
     *  the token is not a decodable JWT. */
    public static String decodeJwtUserId(String jwt) {
        if (jwt == null) return null;
        String[] parts = jwt.split("\\.");
        if (parts.length < 2) return null;
        try {
            String payload = parts[1].replace('-', '+').replace('_', '/');
            while (payload.length() % 4 != 0) payload += "=";
            String json = new String(
                    android.util.Base64.decode(payload, android.util.Base64.URL_SAFE),
                    StandardCharsets.UTF_8);
            Matcher m = Pattern.compile("\"userId\"\\s*:\\s*(\\d+)").matcher(json);
            if (m.find()) return m.group(1);
            Matcher s = Pattern.compile("\"userId\"\\s*:\\s*\"([^\"]+)\"").matcher(json);
            return s.find() ? s.group(1) : null;
        } catch (Exception ignored) {
            return null;
        }
    }

    /** Multipart file upload, proxy first then direct.
     *  Proxy relay: POST /upload?url=<target> with the raw multipart
     *  body forwarded (proxy/app.py v2.3). */
    public static Result postMultipart(String targetUrl, byte[] multipartBody,
                                       String contentType, String bearerToken) {
        // Proxy leg.
        HttpURLConnection c = null;
        try {
            String relayUrl = Proxy.PROXY_BASE + "/upload?url="
                    + java.net.URLEncoder.encode(targetUrl, "UTF-8");
            c = (HttpURLConnection) new URL(relayUrl).openConnection();
            c.setConnectTimeout(30000);
            c.setReadTimeout(120000);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("User-Agent", UA);
            c.setRequestProperty("Content-Type", contentType);
            if (bearerToken != null && !bearerToken.isEmpty()) {
                c.setRequestProperty("Authorization", bearerToken);
            }
            try (OutputStream out = c.getOutputStream()) {
                out.write(multipartBody);
            }
            int code = c.getResponseCode();
            if (code != 404) {
                return new Result(code, readBody(c), "proxy", null);
            }
        } catch (Exception ignored) {
        } finally {
            if (c != null) c.disconnect();
        }
        // Direct leg.
        c = null;
        try {
            c = (HttpURLConnection) new URL(targetUrl).openConnection();
            c.setConnectTimeout(15000);
            c.setReadTimeout(120000);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("User-Agent", UA);
            c.setRequestProperty("Content-Type", contentType);
            if (bearerToken != null && !bearerToken.isEmpty()) {
                c.setRequestProperty("Authorization", bearerToken);
            }
            try (OutputStream out = c.getOutputStream()) {
                out.write(multipartBody);
            }
            int code = c.getResponseCode();
            return new Result(code, readBody(c), "direct", null);
        } catch (Exception e) {
            return new Result(-1, "", "direct", shortErr(e));
        } finally {
            if (c != null) c.disconnect();
        }
    }

    static String jsonQuote(String s) {
        if (s == null) return "\"\"";
        return "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    private static int extractInt(String json, String key) {
        Matcher m = Pattern.compile("\"" + key + "\"\\s*:\\s*(-?\\d+)").matcher(json);
        if (m.find()) {
            try { return Integer.parseInt(m.group(1)); } catch (Exception ignored) {}
        }
        return -1;
    }

    private static String extractString(String json, String key) {
        Matcher m = Pattern.compile("\"" + key + "\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"")
                .matcher(json);
        if (!m.find()) return null;
        return m.group(1).replace("\\\"", "\"").replace("\\\\", "\\")
                .replace("\\n", "\n").replace("\\/", "/");
    }
}
