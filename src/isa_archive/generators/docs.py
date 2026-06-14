import logging
from ..compiler.loader import Registry
from .base import make_jinja_env, prepare_output_dir

logger = logging.getLogger("isa_archive.generators")

def generate_docs(registry: Registry, output_dir: str, doc_format: str = "md"):
    env = make_jinja_env()
    env.globals["hex"] = hex
    out_path = prepare_output_dir(output_dir)

    formats = ["md", "html", "pdf"] if doc_format == "all" else [doc_format]

    for isa_reg in registry.isas.values():
        context = {
            "isa": isa_reg.manifest,
            "operands": isa_reg.operands,
            "schemas": isa_reg.schemas,
            "instructions": isa_reg.instructions,
            "constants": isa_reg.constants,
            "enums": isa_reg.enums,
            "csrs": isa_reg.arch_csrs,
        }

        # Generate each requested format
        for fmt in formats:
            if fmt == "pdf":
                # PDF generation uses the HTML template as source
                template = env.get_template("docs/isa_reference.html.j2")
                html_content = template.render(**context)
                
                try:
                    from weasyprint import HTML
                    filename = f"{isa_reg.name}_reference.pdf"
                    HTML(string=html_content).write_pdf(out_path / filename)
                except ImportError:
                    logger.error("weasyprint not installed — skipping PDF generation")
                except Exception as e:
                    logger.error(f"Error generating PDF: {e}")
            else:
                template_name = f"docs/isa_reference.{fmt}.j2"
                template = env.get_template(template_name)
                output = template.render(**context)
                
                filename = f"{isa_reg.name}_reference.{fmt}"
                with open(out_path / filename, "w") as f:
                    f.write(output)
    
    logger.info(f"Generated {doc_format.upper()} documentation in {output_dir}")
