import jinja2
import pathlib


def make_jinja_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.PackageLoader("isa_archive", "generators/templates"),
        autoescape=jinja2.select_autoescape(),
    )


def prepare_output_dir(output_dir: str) -> pathlib.Path:
    path = pathlib.Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path
