"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";

const NAV_ITEMS = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/requests/new", label: "New Request" },
  { href: "/documents", label: "Documents" },
  { href: "/approvals", label: "Approvals" },
  { href: "/logs", label: "Execution Logs" },
];

export default function Sidebar() {
  const pathname = usePathname();
  const { logout, isAuthenticated } = useAuth();

  if (!isAuthenticated) return null;

  return (
    <nav className="sidebar">
      <h1>AegisForge</h1>
      {NAV_ITEMS.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          className={pathname === item.href ? "active" : ""}
        >
          {item.label}
        </Link>
      ))}
      <div className="spacer" />
      <button
        className="secondary"
        onClick={logout}
        style={{ margin: "0 12px" }}
      >
        Sign Out
      </button>
    </nav>
  );
}
