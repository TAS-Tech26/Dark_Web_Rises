import { createFileRoute, Link } from "@tanstack/react-router";
import { TopNav } from "@/components/dwr/TopNav";
import { DwrButton } from "@/components/dwr/DwrButton";
import { Panel } from "@/components/dwr/Panel";
import { ArrowRight, Cpu, Lock, Trophy, Zap } from "lucide-react";

export const Route = createFileRoute("/")({
  component: Landing,
});


function Landing() {
  return (
    <div className="min-h-screen">
      <TopNav />
      <main className="relative overflow-hidden">
        {/* grid backdrop */}
        <div className="pointer-events-none absolute inset-0 bg-grid opacity-40" />
        <div className="pointer-events-none absolute inset-0 bg-scanlines opacity-30" />

        <section className="relative mx-auto flex min-h-[calc(100vh-64px)] max-w-6xl flex-col items-center justify-center px-6 py-20 text-center">
          <div className="mb-6 inline-flex items-center gap-2 rounded-full border border-neon/40 bg-neon/5 px-4 py-1.5 font-mono text-[10px] uppercase tracking-[0.3em] text-neon">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-neon" />
            PROMPT RELAY • CAPTURE THE FLAG
          </div>

          <h1 className="mb-6 font-mono text-5xl font-black uppercase leading-[0.9] tracking-tight text-foreground sm:text-7xl md:text-8xl">
            Dark<span className="text-neon"> </span>Web
            <br />
            <span className="text-neon" style={{ textShadow: "0 0 40px color-mix(in oklab, var(--neon) 60%, transparent)" }}>
              Rises
            </span>
          </h1>

          <p className="max-w-2xl text-balance text-base text-muted-foreground sm:text-lg">
            Compete in two immersive rounds combining AI image prompting and cybersecurity challenges.
                                  Outsmart every team and climb to the top.
          </p>

          <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
            <Link to="/login">
              <DwrButton size="lg" icon={<ArrowRight className="h-4 w-4" />}>
                ENTER EVENT
              </DwrButton>
            </Link>
            <Link to="/admin/login">
              <DwrButton size="lg" variant="ghost">
                Admin Login
              </DwrButton>
            </Link>
          </div>

          <div className="mt-20 grid w-full max-w-4xl gap-4 sm:grid-cols-3">
            <FeatureCard
              icon={<Cpu className="h-5 w-5" />}
              title="Round 01"
              subtitle="Lost In Translation"
              desc="Four minds. One image. Preserve the original"
            />
            <FeatureCard
              icon={<Lock className="h-5 w-5" />}
              title="Round 02"
              subtitle="Capture the Flag"
              desc="Every gate is a mystery. Every solution brings you closer to victory."
              accent
            />
            <FeatureCard
              icon={<Trophy className="h-5 w-5" />}
              title="Final Standings"
              subtitle="Rise or Fall"
              desc="Only the sharpest ascend. Live leaderboard. No second chances."
            />
          </div>

          <div className="mt-16 flex flex-col items-center gap-3">
            <div className="flex items-center gap-4 font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
              <span>TAMS 2026</span>

              <span className="h-px w-8 bg-border" />

              <span className="flex items-center gap-1.5">
                <Zap className="h-3 w-3 text-neon" />
                System Online
              </span>
            </div>

            <div className="font-mono text-xs uppercase tracking-[0.3em] text-muted-foreground">
              PES UNIVERSITY
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

function FeatureCard({
  icon,
  title,
  subtitle,
  desc,
  accent,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  desc: string;
  accent?: boolean;
}) {
  return (
    <Panel className="p-5 text-left" glow={accent ? "magenta" : undefined}>
      <div className="mb-3 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
        <span className={accent ? "text-magenta" : "text-neon"}>{icon}</span>
        {title}
      </div>
      <div className="mb-2 font-mono text-lg font-bold text-foreground">{subtitle}</div>
      <p className="text-sm text-muted-foreground">{desc}</p>
    </Panel>
  );
}
