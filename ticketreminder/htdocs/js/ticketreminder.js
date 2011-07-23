$(document).ready(function () {
	$("fieldset").filter(":has(legend input)").each(function (i, el) {
		$(el).find("*").focus(function () {
			$(el).find("legend input").attr("checked", "checked");
		});
		$(el).find(".field input:radio").click(function () {
			$(el).find("legend input").attr("checked", "checked");
		});
	});
});
