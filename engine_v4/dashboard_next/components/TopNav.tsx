'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

// Primary nav: 3 mcap buckets scoped to the user's 71-ticker watchlist
// (data/picks_77.json). Each page filters patrol + conviction + staging
// signals to its bucket so the firehose narrows to ~15-30 names.
// Old views (Triggered / Pre-Break / Watchlist / Continuation / Today)
// remain available via secondary row below.
const NAV = [
  { href: '/mega',  label: '🐳 Mega',  sub: 'large-cap watchlist' },
  { href: '/mid',   label: '🐬 Mid',   sub: 'mid-cap watchlist' },
  { href: '/small', label: '🐟 Small', sub: 'small-cap watchlist' },
];

const SECONDARY_NAV = [
  { href: '/breakout',     label: '🚀 Triggered',    sub: 'fired today' },
  { href: '/prebreakout',  label: '📈 Pre-Break',    sub: 'setups coiling' },
  { href: '/conviction',   label: '👁 Watchlist',    sub: 'setups' },
  { href: '/continuation', label: '🏃 Continuation', sub: 'multi-day chains' },
  { href: '/today',        label: '⚡ Today',        sub: 'one trade or pass' },
];

export default function TopNav() {
  const pathname = usePathname();
  return (
    <>
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
      <nav className="topnav topnav-secondary" role="navigation"
           style={{ opacity: 0.7, fontSize: '0.85em', padding: '4px 0' }}>
        {SECONDARY_NAV.map((item) => {
          const active = pathname === item.href || pathname?.startsWith(item.href + '/');
          return (
            <Link key={item.href} href={item.href} className={active ? 'active' : ''}>
              {item.label} <span className="sub">{item.sub}</span>
            </Link>
          );
        })}
      </nav>
    </>
  );
}
