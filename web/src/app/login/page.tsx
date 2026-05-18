"use client";

import { useState } from "react";
import { supabaseBrowser } from "@/lib/supabase/client";

export default function LoginPage() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function signIn() {
    setLoading(true);
    setError(null);
    const supabase = supabaseBrowser();
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "slack_oidc",
      options: {
        redirectTo: `${window.location.origin}/auth/callback`,
        scopes: "openid email profile",
      },
    });
    if (error) {
      setError(error.message);
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-6">
      <div className="w-full max-w-sm border border-border rounded-2xl bg-card p-8 shadow-sm">
        <h1 className="text-2xl font-semibold mb-2">Gastrobrain</h1>
        <p className="text-sm text-muted-foreground mb-8">
          Gastroduceの社内ナレッジに、自然な日本語で質問できます。
        </p>
        <button
          onClick={signIn}
          disabled={loading}
          className="w-full h-11 rounded-lg bg-primary text-primary-foreground font-medium hover:opacity-90 transition disabled:opacity-50"
        >
          {loading ? "Slackへ移動中..." : "Slackでログイン"}
        </button>
        {error && (
          <p className="text-sm text-destructive mt-4">{error}</p>
        )}
        <p className="text-xs text-muted-foreground mt-6 leading-relaxed">
          ログインするとSlackワークスペースのメンバーシップを基にアクセスが許可されます。@gastroduce-japan.co.jp 以外のアカウントは拒否されます。
        </p>
      </div>
    </div>
  );
}
