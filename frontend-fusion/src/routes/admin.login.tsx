import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { TopNav } from "@/components/dwr/TopNav";
import { Panel } from "@/components/dwr/Panel";
import { DwrButton } from "@/components/dwr/DwrButton";
import { DwrInput, Field } from "@/components/dwr/Form";
import { useGame } from "@/lib/game-connection";
import {
  ShieldAlert,
  ArrowRight,
  ArrowLeft,
  Loader2,
} from "lucide-react";

export const Route = createFileRoute("/admin/login")({
  component: AdminLogin,
});

function AdminLogin() {
  const navigate = useNavigate();
  const game = useGame();

  const [id, setId] = useState("");
  const [pw, setPw] = useState("");

  useEffect(() => {
    if (game.adminId !== null) {
      navigate({ to: "/admin/dashboard" });
    }
  }, [game.adminId, navigate]);


  return (
    <div className="min-h-screen">
      <TopNav />

      <main className="relative mx-auto flex min-h-[calc(100vh-64px)] max-w-md flex-col justify-center px-4 py-12">

        {/* Background */}
        <div className="pointer-events-none absolute inset-0 bg-grid opacity-30" />

        {/* Back Button */}
        <button
          onClick={() => navigate({ to: "/" })}
          className="mb-6 flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-muted-foreground transition-colors hover:text-magenta"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Home
        </button>

        <Panel
          variant="elevated"
          className="relative p-8"
          glow="magenta"
        >
          {/* Header */}
          <div className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.3em] text-magenta">
            <ShieldAlert className="h-3 w-3" />
            Admin Console
          </div>

          <h1 className="mb-2 font-mono text-3xl font-black uppercase tracking-tight text-foreground">
            Restricted <span className="text-magenta">Access</span>
          </h1>

          <p className="mb-8 text-sm text-muted-foreground">
            Authorized event administrators only. All operations are monitored
            and logged.
          </p>

          {/* Login Form */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              game.login(id.trim(), pw);
            }}
            className="space-y-5"
          >
            <Field label="Admin ID">
              <DwrInput
                value={id}
                onChange={(e) => setId(e.target.value)}
                placeholder="admin@dwr"
                autoComplete="username"
                required
              />
            </Field>

            <Field label="Password">
              <DwrInput
                type="password"
                value={pw}
                onChange={(e) => setPw(e.target.value)}
                placeholder="Enter Password"
                autoComplete="current-password"
                required
              />
            </Field>

            <DwrButton
              type="submit"
              size="lg"
              variant="magenta"
              className="w-full"
              disabled={game.loggingIn || !game.connected}
              icon={
                game.loggingIn ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <ArrowRight className="h-4 w-4" />
                )
              }
            >
              {game.loggingIn
                ? "Authenticating..."
                : game.connected
                  ? "Login to Dashboard"
                  : "Connecting to server..."}
            </DwrButton>

            {(game.loginError || game.socketError) && (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-center text-sm text-destructive">
                {game.loginError || game.socketError}
              </div>
            )}
          </form>


          {/* Security Notice */}
          <div className="mt-8 rounded-lg border border-magenta/20 bg-magenta/5 p-4">
            <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-magenta">
              Security Status
            </div>

            <div className="mt-2 flex items-center gap-2 font-mono text-sm text-magenta">
              <span className="h-2 w-2 animate-pulse rounded-full bg-magenta" />
              Administrative Access Only
            </div>
          </div>

          {/* Footer */}
          <div className="mt-8 border-t border-border pt-4 text-center font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            DARK WEB RISES • ADMIN CONSOLE • TAMS 2026
          </div>
        </Panel>
      </main>
    </div>
  );
}