import { useEffect, useState } from "react";
import { api, token, type Session } from "./api";

// Self-contained sign-in + sim-credits control for the header. Owns its own state; the only
// coupling to App is the "fg:credits" window event App fires after each run so the balance updates.
export function AuthBar() {
  const [session, setSession] = useState<Session | null>(null);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [buying, setBuying] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Restore a session from a stored token on load.
  useEffect(() => {
    if (token.get()) api.me().then(setSession).catch(() => token.set(null));
  }, []);

  // App dispatches fg:credits with the remaining balance after every run.
  useEffect(() => {
    const onCredits = (e: Event) => {
      const c = (e as CustomEvent).detail;
      if (typeof c === "number") setSession((s) => (s ? { ...s, credits: c } : s));
    };
    window.addEventListener("fg:credits", onCredits);
    return () => window.removeEventListener("fg:credits", onCredits);
  }, []);

  const submit = async () => {
    if (!email || !password) return;
    setBusy(true); setErr(null);
    try {
      const res = mode === "login"
        ? await api.login(email, password)
        : await api.signup(email, password, name || undefined);
      token.set(res.access_token);
      setSession({ user: res.user, credits: res.credits });
      setOpen(false); setPassword("");
    } catch (e: any) { setErr(e.message || "Failed"); }
    finally { setBusy(false); }
  };

  const logout = () => { token.set(null); setSession(null); };
  const buy = async () => {
    setBuying(true);
    try { const r = await api.buyCredits(); setSession((s) => (s ? { ...s, credits: r.balance } : s)); }
    catch (e: any) { alert("Buy failed: " + e.message); }
    finally { setBuying(false); }
  };

  if (session) {
    const low = session.credits <= 0;
    return (
      <div className="authbar">
        <span className={"credits" + (low ? " low" : "")} title="Sim credits — each run costs 1">
          ◈ {session.credits} {session.credits === 1 ? "credit" : "credits"}
        </span>
        <button className="primary" disabled={buying} onClick={buy}>{buying ? "…" : "＋ Buy 20"}</button>
        <span className="who" title={session.user.email}>{session.user.display_name || session.user.email}</span>
        <button onClick={logout}>Sign out</button>
      </div>
    );
  }

  return (
    <div className="authbar">
      {!open && <button onClick={() => { setOpen(true); setErr(null); }}>Sign in</button>}
      {open && (
        <div className="authpop">
          <div className="tabs">
            <button className={mode === "login" ? "active" : ""} onClick={() => { setMode("login"); setErr(null); }}>Log in</button>
            <button className={mode === "signup" ? "active" : ""} onClick={() => { setMode("signup"); setErr(null); }}>Sign up</button>
          </div>
          {mode === "signup" && (
            <input placeholder="display name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
          )}
          <input placeholder="email" type="email" autoComplete="email" value={email}
            onChange={(e) => setEmail(e.target.value)} />
          <input placeholder="password" type="password" value={password}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()} />
          {err && <div className="autherr">{err}</div>}
          <div className="authrow">
            <button className="primary" disabled={busy} onClick={submit}>
              {busy ? "…" : mode === "login" ? "Log in" : "Create account"}
            </button>
            <button onClick={() => setOpen(false)}>Cancel</button>
          </div>
          <div className="hint">
            {mode === "signup"
              ? "8+ chars, upper/lower/number/symbol. Get 5 free sim credits."
              : "Signed-in runs spend 1 sim credit each."}
          </div>
        </div>
      )}
    </div>
  );
}
