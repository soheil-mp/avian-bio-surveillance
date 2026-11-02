import { AlertTriangle, LoaderCircle } from "lucide-react";

type AlertsLoadingStateProps = {
  label: string;
  tone?: "loading" | "error";
  className?: string;
};

export default function AlertsLoadingState({
  label,
  tone = "loading",
  className = "",
}: AlertsLoadingStateProps) {
  const Icon = tone === "loading" ? LoaderCircle : AlertTriangle;

  return (
    <div className={`flex items-center justify-center px-6 ${className}`.trim()}>
      <div
        className="inline-flex items-center gap-3 rounded-2xl px-5 py-4"
        style={{
          background: "rgba(16,16,16,0.94)",
          border:
            tone === "loading"
              ? "1px solid rgba(255,255,255,0.08)"
              : "1px solid rgba(248,113,113,0.24)",
          boxShadow: "0 22px 54px rgba(0,0,0,0.3)",
        }}
      >
        <Icon
          className={tone === "loading" ? "h-4 w-4 animate-spin" : "h-4 w-4"}
          style={{
            color:
              tone === "loading"
                ? "rgba(255,255,255,0.72)"
                : "rgba(252,165,165,0.92)",
          }}
        />
        <span
          className="text-sm font-medium tracking-[-0.01em]"
          style={{ color: "rgba(255,255,255,0.9)" }}
        >
          {label}
        </span>
      </div>
    </div>
  );
}