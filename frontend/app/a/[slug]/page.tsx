// Placeholder. The slug will select branding and the org for sign-up and login only;
// it never establishes identity.
export default async function OrgPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-2 p-6">
      <h1 className="text-2xl font-semibold">{slug}</h1>
      <p className="text-gray-600">Chat coming soon.</p>
    </main>
  );
}
