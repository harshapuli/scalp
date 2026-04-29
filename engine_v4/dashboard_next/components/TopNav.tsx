'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

// Nav scoped to the user's 71-ticker watchlist (data/picks_77.json),
// split into 3 mcap buckets. Each page filters patrol + conviction +
// staging signals to its bucket so the firehose narrows to ~15-30 names.
// Old views (Triggered / Pre-Break / Watchlist / Continuation / Today)
// removed at user request — too noisy for daily decision-making.
// Pages still exist at /breakout, /prebreakout etc. — direct URL only.
const NAV = [
  { href: '/mega',  label: '🐳 Mega',  sub: 'large-cap watchlist' },
  { href: '/mid',   label: '🐬 Mid',   sub: 'mid-cap watchlist' },
  { href: '/small', label: '🐟 Small', sub: 'small-cap watchlist' },
];

export default function TopNav() {
  const pathname = usePathname();
  return (
    <nav className="topnav" role="navigation">
      {NAV.map((item) => {
        const active = pathname === item.href || pathname?.startsWith(item.href + '/');
        return (
          <Link key={item.href} href={item.href} className={active ? 'active' : ''}>
            {item.label} <span className="sub">{item.sub}</span>
          </Link>
        );
      })}
    </nav>
  );
}
