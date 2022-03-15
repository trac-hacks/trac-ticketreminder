
(function ($) {

  $(document).ready(function ($) {
    $("#attachments").after(trdata.tags);
    $("#reminders").toggleClass("collapsed");
    $(".trac-nav").prepend(' <a href="#reminders" id="trac-up-reminders" title="Go to the list of reminders">Reminders</a> &uarr;');
    $("#trac-up-reminders").click(function () {
      $("#reminders").removeClass("collapsed");
      return true;
    });
  });

})(jQuery);

