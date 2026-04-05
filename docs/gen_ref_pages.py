import pathlib
import shutil
import mkdocs_gen_files

# Map of source files to their destination in the docs
ROOT_FILES = {
    "CHANGELOG.md": "CHANGELOG.md",
    "CONTRIBUTING.md": "CONTRIBUTING.md",
    "SECURITY.md": "SECURITY.md",
    "LICENSE": "LICENSE.md",
}

# Copy root files and fix links
for src, dst in ROOT_FILES.items():
    with open(src, "r") as f:
        content = f.read()
        # Fix links like [Adding an Agent](docs/adding-an-agent.md) -> [Adding an Agent](adding-an-agent.md)
        content = content.replace("docs/", "")
        # Fix relative links to root files that are now in the same directory
        content = content.replace("(LICENSE)", "(LICENSE.md)")
        
        with mkdocs_gen_files.open(dst, "w") as fd:
            fd.write(content)

# Generate pages for agents
agents_dir = pathlib.Path("agents")
for agent_path in agents_dir.iterdir():
    if agent_path.is_dir():
        readme_path = agent_path / "README.md"
        if readme_path.exists():
            with open(readme_path, "r") as f:
                content = f.read()
                
                # Fix links like ../../docs/adr/002-agent-tool-vs-sub-agents.md -> ../adr/002-agent-tool-vs-sub-agents.md
                content = content.replace("../../docs/", "../")
                # Fix links like ../../README.md#environment-configuration -> ../index.md#environment-configuration
                content = content.replace("../../README.md", "../index.md")
                # Fix links to other agents like ../kafka-health/ -> kafka-health.md
                for other_agent in agents_dir.iterdir():
                    if other_agent.is_dir():
                        content = content.replace(f"../{other_agent.name}/", f"{other_agent.name}.md")

                with mkdocs_gen_files.open(f"agents/{agent_path.name}.md", "w") as fd:
                    fd.write(content)
            
            # Copy assets if they exist
            assets_src = agent_path / "assets"
            if assets_src.exists():
                for asset in assets_src.iterdir():
                    asset_dst = pathlib.Path("agents/assets") / asset.name
                    with open(asset, "rb") as f:
                        with mkdocs_gen_files.open(asset_dst, "wb") as fd:
                            fd.write(f.read())

# Generate page for core
core_readme = pathlib.Path("core/README.md")
if core_readme.exists():
    with open(core_readme, "r") as f:
        content = f.read()
        # Fix links
        content = content.replace("../docs/", "../")
        
        with mkdocs_gen_files.open("core/README.md", "w") as fd:
            fd.write(content)
            
    # Copy core assets
    core_assets_src = pathlib.Path("core/assets")
    if core_assets_src.exists():
        for asset in core_assets_src.iterdir():
            asset_dst = pathlib.Path("core/assets") / asset.name
            with open(asset, "rb") as f:
                with mkdocs_gen_files.open(asset_dst, "wb") as fd:
                    fd.write(f.read())
