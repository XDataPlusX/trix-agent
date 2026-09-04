import { useLayoutEffect } from "react";
import { BookOpen } from "lucide-react";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn } from "@/lib/utils";
import { PluginSlot } from "@/plugins";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";

// There is no hosted documentation site for this build. Trix Agent ships
// its product reference as a bundled skill (`trix-agent`) that loads
// directly into the agent's context, so "ask the agent" *is* the docs —
// see this page's static panel below instead of an external link/iframe.

export default function DocsPage() {
  const { t } = useI18n();
  const { setEnd } = usePageHeader();

  useLayoutEffect(() => {
    setEnd(null);
    return () => {
      setEnd(null);
    };
  }, [setEnd]);

  return (
    <div
      className={cn(
        "flex min-h-0 w-full min-w-0 flex-1 flex-col",
        "pt-1 sm:pt-2",
      )}
    >
      <PluginSlot name="docs:top" />
      <div className="min-h-0 w-full min-w-0 flex-1 overflow-y-auto">
        <Card>
          <CardContent className="py-12">
            <div className="mx-auto flex max-w-2xl flex-col items-center gap-3 text-center text-sm text-muted-foreground">
              <BookOpen className="h-8 w-8 opacity-40" />
              <h2 className="font-mondwest text-display text-base tracking-wider text-foreground">
                {t.app.nav.documentation}
              </h2>
              <p>
                Trix Agent doesn't ship a separate documentation site. Ask
                the agent directly — it loads a bundled skill (
                <span className="font-mono">trix-agent</span>) that holds
                the full product reference: setup, configuration, skins,
                skills, MCP servers, and every surface it runs on.
              </p>
              <p>
                Open a chat and ask something like{" "}
                <span className="font-mono">
                  "how do I configure a new model provider?"
                </span>{" "}
                and the agent will answer from that reference.
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
      <PluginSlot name="docs:bottom" />
    </div>
  );
}
