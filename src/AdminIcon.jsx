const paths = {
  overview: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  customers: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M16 4a4 4 0 0 1 0 8 M22 21v-2a4 4 0 0 0-3-3.87 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  tasks: 'm12 3 9 5v9l-9 5-9-5V8z M3 8l9 5 9-5 M12 13v9',
  support: 'M21 11a9 9 0 0 1-13 8L3 21l2-5a9 9 0 1 1 16-5z M8 10h8 M8 14h4',
  orders: 'M6 3h12v18l-3-2-3 2-3-2-3 2z M9 7h6 M9 11h6 M9 15h3',
  credits: 'M3 5h17v15H3z M3 8h17 M15 12h6v5h-6z',
  settlements: 'M7 3h10v4H7z M7 5H4v16h16V5h-3 M8 12l2 2 5-5 M8 18h8',
  refunds: 'M3 9h12a6 6 0 0 1 0 12 M7 5 3 9l4 4',
  invoices: 'M5 3h10l4 4v14H5z M14 3v5h5 M8 12h8 M8 16h8',
  usage: 'M4 20V4 M4 20h17 M8 16v-5 M12 16V7 M16 16v-3 M20 16V4',
  packages: 'm12 3 9 5-9 5-9-5z M3 8v9l9 5 9-5V8 M12 13v9 M7 5.8l9 5',
  policy: 'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
  users: 'M12 3 3 7v6c0 5 9 9 9 9s9-4 9-9V7z M9 12l2 2 4-4',
  terms: 'M5 3h14v18H5z M8 7h8 M8 11h8 M8 15h5',
  system: 'M3 4h18v12H3z M8 21h8 M12 16v5 M6 11h3l2-4 3 6 2-3h2',
  audit: 'M21 12a9 9 0 1 1-3-6.7 M21 3v6h-6 M12 7v5l3 2',
  search: 'M21 21l-5-5 M18 10.5a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0',
  menu: 'M4 6h16 M4 12h16 M4 18h16',
  external: 'M15 3h6v6 M10 14 21 3 M9 3H3v18h18v-6',
  panel: 'M3 4h18v16H3z M9 4v16 M6 8v8',
}
export default function AdminIcon({ name, size = 18 }) {
  return <svg className="admin-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.overview} /></svg>
}
